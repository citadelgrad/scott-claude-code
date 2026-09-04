"""Pairwise write-scope conflict detection for Beads ready-front candidates.

Pure computation, no I/O and no ``bd`` invocation: classifies declared
write scopes (literal paths and glob patterns) for every candidate pair
as ``safe``, ``conflict``, or ``unknown``. Implements AC-T11-003.

Design constraints (by construction, not by runtime check):

* No parameter, field, or code path here ever models a "line range".
  This module can never claim semantic merge safety from disjoint line
  ranges alone, because it has no representation of line ranges at all.
* Symlinks and ``..`` traversal in literal (non-glob) scope entries are
  resolved against ``repository_root`` before comparison. A scope entry
  that cannot be resolved, or that resolves outside the repository
  root, is always classified defensively (never ``safe``).
* A broad glob (e.g. ``src/**``) conflicts with a narrower descendant
  scope (e.g. ``src/foo/bar.py``) via directory-component containment.
* Known shared-manifest hotspots (``uv.lock``, ``package-lock.json``,
  etc.) get a dedicated ``SHARED_MANIFEST_HOTSPOT`` reason when both
  sides declare the same hotspot filename.
* Two literal paths that are distinct directory entries for the same
  underlying file (a hardlink alias) are never ``safe``: when both
  paths exist, device/inode identity is compared in addition to
  realpath containment, so a hardlink alias is classified ``conflict``
  with reason ``HARDLINK_ALIAS`` even though the normalized path
  strings differ. Symlink aliases are already caught by realpath
  resolution collapsing both entries to the same normalized path.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "beads.conflict-matrix.v1"

_GLOB_CHARS = frozenset("*?[")

_SHARED_MANIFEST_HOTSPOTS = frozenset(
    {
        "uv.lock",
        "package-lock.json",
        "poetry.lock",
        "Cargo.lock",
        "go.sum",
        "Gemfile.lock",
        "yarn.lock",
        "pnpm-lock.yaml",
    }
)

_VERDICT_ORDER = {"safe": 0, "unknown": 1, "conflict": 2}


class ConflictDetectionError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code if not message else f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class NormalizedScope:
    raw: str
    normalized: str
    is_glob: bool
    contained: bool  # False => escaped repository root or unresolvable
    inode: tuple[int, int] | None = None  # (st_dev, st_ino); None if unknown


def _is_glob(raw: str) -> bool:
    return any(ch in _GLOB_CHARS for ch in raw)


def _lexical_normalize(raw: str) -> str:
    posix = raw.replace("\\", "/")
    collapsed = posixpath.normpath(posix)
    if collapsed == ".":
        return ""
    return collapsed.lstrip("/")


def _components(normalized: str) -> list[str]:
    return [c for c in normalized.split("/") if c not in ("", ".")]


def _strip_glob_suffix(components: Sequence[str]) -> list[str]:
    prefix: list[str] = []
    for comp in components:
        if any(ch in _GLOB_CHARS for ch in comp):
            break
        prefix.append(comp)
    return prefix


def _is_prefix(prefix: Sequence[str], other: Sequence[str]) -> bool:
    if len(prefix) > len(other):
        return False
    return list(other[: len(prefix)]) == list(prefix)


def normalize_path(repository_root: str, raw: str) -> NormalizedScope:
    """Normalize one declared write-scope entry against ``repository_root``.

    Glob patterns are normalized lexically only (no filesystem probing,
    since they do not name a single existing path). Literal paths are
    resolved via ``realpath`` and checked for containment within
    ``repository_root``; escaping or unresolvable paths come back with
    ``contained=False`` so callers never classify them as ``safe``.
    """
    if not raw or "\x00" in raw:
        raise ConflictDetectionError("SCOPE_PATH_INVALID", raw)
    if _is_glob(raw):
        return NormalizedScope(raw, _lexical_normalize(raw), True, True)

    root = os.path.realpath(repository_root)
    candidate = raw if os.path.isabs(raw) else os.path.join(repository_root, raw)
    lexical = _lexical_normalize(raw if not os.path.isabs(raw) else raw.lstrip("/"))
    try:
        resolved = os.path.realpath(candidate)
    except OSError:
        return NormalizedScope(raw, lexical, False, False)
    try:
        relative = os.path.relpath(resolved, root)
    except ValueError:
        return NormalizedScope(raw, lexical, False, False)
    if relative == os.curdir:
        relative = ""
    escaped = relative == os.pardir or relative.startswith(os.pardir + os.sep)
    normalized = relative.replace(os.sep, "/")
    contained = not escaped
    inode: tuple[int, int] | None = None
    if contained:
        try:
            status = os.stat(resolved)
        except OSError:
            inode = None
        else:
            inode = (status.st_dev, status.st_ino)
    return NormalizedScope(raw, normalized, False, contained, inode)


def _hotspot_overlap(a: NormalizedScope, b: NormalizedScope) -> str | None:
    comps_a = _components(a.normalized)
    comps_b = _components(b.normalized)
    name_a = comps_a[-1] if comps_a else ""
    name_b = comps_b[-1] if comps_b else ""
    if name_a and name_a == name_b and name_a in _SHARED_MANIFEST_HOTSPOTS:
        return name_a
    return None


def _classify_entry_pair(
    a: NormalizedScope, b: NormalizedScope
) -> tuple[str, str, str | None]:
    if not a.contained or not b.contained:
        return "unknown", "SCOPE_ESCAPES_ROOT", None

    if (
        a.inode is not None
        and b.inode is not None
        and a.inode == b.inode
        and a.normalized != b.normalized
    ):
        # Same (st_dev, st_ino) under two different declared names: a
        # hardlink alias. The normalized-path-prefix logic below would
        # otherwise call this "disjoint", but both names write through
        # to identical underlying content -- never safe.
        return "conflict", "HARDLINK_ALIAS", a.normalized

    hotspot = _hotspot_overlap(a, b)
    if hotspot is not None:
        return "conflict", "SHARED_MANIFEST_HOTSPOT", hotspot

    comps_a = _components(a.normalized)
    comps_b = _components(b.normalized)
    prefix_a = _strip_glob_suffix(comps_a) if a.is_glob else comps_a
    prefix_b = _strip_glob_suffix(comps_b) if b.is_glob else comps_b

    if a.is_glob and not prefix_a:
        return "unknown", "SCOPE_PATTERN_AMBIGUOUS", None
    if b.is_glob and not prefix_b:
        return "unknown", "SCOPE_PATTERN_AMBIGUOUS", None

    if _is_prefix(prefix_a, prefix_b) or _is_prefix(prefix_b, prefix_a):
        shorter = prefix_a if len(prefix_a) <= len(prefix_b) else prefix_b
        return "conflict", "PATH_OVERLAP", "/".join(shorter) or "."

    return "safe", "DISJOINT_SCOPE", None


def classify_pair(
    scopes_a: Sequence[str], scopes_b: Sequence[str], repository_root: str
) -> tuple[str, str, list[str]]:
    """Classify one candidate pair's declared write scopes.

    Returns ``(verdict, reason, overlaps)`` where ``verdict`` is the
    worst verdict found across every (entry_a, entry_b) combination:
    a single conflicting or unknown entry pair makes the whole
    candidate pair conflicting/unknown, matching "unknown/conflicting
    pairs cannot share a batch".
    """
    if not scopes_a or not scopes_b:
        return "unknown", "SCOPE_UNDECLARED", []

    worst = "safe"
    worst_reason = "DISJOINT_SCOPE"
    overlaps: list[str] = []

    for raw_a in scopes_a:
        norm_a = normalize_path(repository_root, raw_a)
        for raw_b in scopes_b:
            norm_b = normalize_path(repository_root, raw_b)
            verdict, reason, overlap = _classify_entry_pair(norm_a, norm_b)
            if overlap is not None:
                overlaps.append(overlap)
            if _VERDICT_ORDER[verdict] > _VERDICT_ORDER[worst]:
                worst, worst_reason = verdict, reason

    return worst, worst_reason, overlaps


def _pair_key(a: str, b: str) -> str:
    return f"{a}|{b}" if a <= b else f"{b}|{a}"


def build_conflict_matrix(
    candidates: Sequence[Mapping[str, Any]], repository_root: str
) -> dict[str, Any]:
    """Build a deterministic pairwise conflict matrix across ``candidates``.

    Each candidate is ``{"id": str, "scopes": [str, ...]}``. The result
    is JSON-serializable and orders pairs by candidate declaration
    order (stable, deterministic).
    """
    ids: list[str] = []
    scopes: dict[str, list[str]] = {}
    for candidate in candidates:
        issue_id = str(candidate["id"])
        if issue_id in scopes:
            raise ConflictDetectionError("DUPLICATE_CANDIDATE_ID", issue_id)
        ids.append(issue_id)
        scopes[issue_id] = [str(s) for s in candidate.get("scopes", [])]

    pairs = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            verdict, reason, overlap = classify_pair(
                scopes[a], scopes[b], repository_root
            )
            pairs.append(
                {
                    "a": a,
                    "b": b,
                    "pair_key": _pair_key(a, b),
                    "verdict": verdict,
                    "reason": reason,
                    "overlap": overlap,
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "repository_root": repository_root,
        "candidate_ids": ids,
        "pairs": pairs,
    }


def index_by_pair_key(conflict_matrix: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Convert a ``build_conflict_matrix`` result into a pair-key index.

    This is the shape ``build_ready_front.evaluate``'s ``conflict_matrix``
    parameter expects; kept as a separate helper so ``build_ready_front``
    never needs to import this module.
    """
    index: dict[str, dict[str, Any]] = {}
    for pair in conflict_matrix.get("pairs", []):
        index[pair["pair_key"]] = {"verdict": pair["verdict"], "reason": pair["reason"]}
    return index
