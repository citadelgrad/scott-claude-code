from __future__ import annotations

from pathlib import Path

from ._common import load  # noqa: E402


def _mod():
    return load("detect_write_conflicts")


def test_disjoint_literal_paths_are_safe(tmp_path: Path) -> None:
    m = _mod()
    verdict, reason, overlap = m.classify_pair(
        ["src/a.py"], ["src/b.py"], str(tmp_path)
    )
    assert verdict == "safe"
    assert reason == "DISJOINT_SCOPE"
    assert overlap == []


def test_identical_literal_path_conflicts(tmp_path: Path) -> None:
    m = _mod()
    verdict, reason, overlap = m.classify_pair(
        ["src/a.py"], ["src/a.py"], str(tmp_path)
    )
    assert verdict == "conflict"
    assert reason == "PATH_OVERLAP"
    assert overlap == ["src/a.py"]


def test_broad_glob_conflicts_with_narrower_descendant(tmp_path: Path) -> None:
    m = _mod()
    verdict, reason, _ = m.classify_pair(["src/**"], ["src/foo/bar.py"], str(tmp_path))
    assert verdict == "conflict"
    assert reason == "PATH_OVERLAP"
    # symmetric
    verdict2, reason2, _ = m.classify_pair(
        ["src/foo/bar.py"], ["src/**"], str(tmp_path)
    )
    assert verdict2 == "conflict"
    assert reason2 == "PATH_OVERLAP"


def test_sibling_glob_directories_are_safe(tmp_path: Path) -> None:
    m = _mod()
    verdict, reason, _ = m.classify_pair(
        ["src/alpha/**"], ["src/beta/**"], str(tmp_path)
    )
    assert verdict == "safe"
    assert reason == "DISJOINT_SCOPE"


def test_top_level_wildcard_pattern_is_unknown_not_safe(tmp_path: Path) -> None:
    m = _mod()
    verdict, reason, _ = m.classify_pair(["*.lock"], ["src/a.py"], str(tmp_path))
    assert verdict == "unknown"
    assert reason == "SCOPE_PATTERN_AMBIGUOUS"


def test_shared_manifest_hotspot_flagged_distinctly(tmp_path: Path) -> None:
    m = _mod()
    verdict, reason, overlap = m.classify_pair(["uv.lock"], ["uv.lock"], str(tmp_path))
    assert verdict == "conflict"
    assert reason == "SHARED_MANIFEST_HOTSPOT"
    assert overlap == ["uv.lock"]


def test_symlink_escaping_repository_root_is_never_safe(tmp_path: Path) -> None:
    m = _mod()
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    link = repo / "escape"
    link.symlink_to(outside)
    verdict, reason, _ = m.classify_pair(["escape"], ["escape"], str(repo))
    assert verdict == "unknown"
    assert reason == "SCOPE_ESCAPES_ROOT"


def test_dot_dot_traversal_is_never_safe(tmp_path: Path) -> None:
    m = _mod()
    repo = tmp_path / "repo"
    repo.mkdir()
    verdict, reason, _ = m.classify_pair(
        ["../outside.txt"], ["../outside.txt"], str(repo)
    )
    assert verdict == "unknown"
    assert reason == "SCOPE_ESCAPES_ROOT"


def test_hardlink_alias_is_never_safe(tmp_path: Path) -> None:
    m = _mod()
    repo = tmp_path
    (repo / "real.py").write_text("x = 1\n")
    import os

    os.link(repo / "real.py", repo / "alias.py")
    verdict, reason, overlap = m.classify_pair(["real.py"], ["alias.py"], str(repo))
    # Hardlinks share (st_dev, st_ino) despite being distinct directory
    # entries with distinct normalized path strings; two declared names
    # that write through to identical underlying content must never be
    # "disjoint scope" -- AC-T11-003 requires aliased scopes to
    # serialize or block, so this is a conflict, not a coincidence.
    assert verdict == "conflict"
    assert reason == "HARDLINK_ALIAS"
    assert overlap == ["real.py"]


def test_symlink_alias_within_root_conflicts_with_its_real_target(
    tmp_path: Path,
) -> None:
    m = _mod()
    repo = tmp_path
    (repo / "real.py").write_text("x = 1\n")
    (repo / "alias.py").symlink_to(repo / "real.py")
    # Distinct declared names, one a symlink to the other, both fully
    # inside the repository root: realpath resolution collapses them to
    # the identical normalized path, so this is caught as an ordinary
    # PATH_OVERLAP -- distinct from the SCOPE_ESCAPES_ROOT case, where
    # the symlink target lands outside the repository entirely.
    verdict, reason, overlap = m.classify_pair(["real.py"], ["alias.py"], str(repo))
    assert verdict == "conflict"
    assert reason == "PATH_OVERLAP"
    assert overlap == ["real.py"]


def test_undeclared_scope_is_unknown(tmp_path: Path) -> None:
    m = _mod()
    verdict, reason, _ = m.classify_pair([], ["src/a.py"], str(tmp_path))
    assert verdict == "unknown"
    assert reason == "SCOPE_UNDECLARED"


def test_empty_or_null_byte_scope_entry_raises(tmp_path: Path) -> None:
    m = _mod()
    import pytest

    with pytest.raises(m.ConflictDetectionError, match="SCOPE_PATH_INVALID"):
        m.normalize_path(str(tmp_path), "")
    with pytest.raises(m.ConflictDetectionError, match="SCOPE_PATH_INVALID"):
        m.normalize_path(str(tmp_path), "a\x00b")


def test_build_conflict_matrix_is_deterministic_and_pairwise(tmp_path: Path) -> None:
    m = _mod()
    candidates = [
        {"id": "A", "scopes": ["src/a.py"]},
        {"id": "B", "scopes": ["src/b.py"]},
        {"id": "C", "scopes": ["src/a.py"]},
    ]
    matrix = m.build_conflict_matrix(candidates, str(tmp_path))
    assert matrix["schema_version"] == "beads.conflict-matrix.v1"
    assert matrix["candidate_ids"] == ["A", "B", "C"]
    pair_map = {(p["a"], p["b"]): p["verdict"] for p in matrix["pairs"]}
    assert pair_map[("A", "B")] == "safe"
    assert pair_map[("A", "C")] == "conflict"
    assert pair_map[("B", "C")] == "safe"
    # deterministic: rebuilding produces byte-identical structure
    matrix2 = m.build_conflict_matrix(candidates, str(tmp_path))
    assert matrix == matrix2


def test_build_conflict_matrix_rejects_duplicate_candidate_ids(tmp_path: Path) -> None:
    m = _mod()
    import pytest

    candidates = [
        {"id": "A", "scopes": ["src/a.py"]},
        {"id": "A", "scopes": ["src/b.py"]},
    ]
    with pytest.raises(m.ConflictDetectionError, match="DUPLICATE_CANDIDATE_ID"):
        m.build_conflict_matrix(candidates, str(tmp_path))


def test_index_by_pair_key_round_trips_matrix(tmp_path: Path) -> None:
    m = _mod()
    candidates = [
        {"id": "A", "scopes": ["src/a.py"]},
        {"id": "B", "scopes": ["src/a.py"]},
    ]
    matrix = m.build_conflict_matrix(candidates, str(tmp_path))
    index = m.index_by_pair_key(matrix)
    assert index["A|B"]["verdict"] == "conflict"


def test_module_never_models_a_line_range() -> None:
    """Structural guarantee backing the spec's explicit prohibition:
    'never claim semantic merge safety from disjoint line ranges alone'.

    Checks actual code identifiers only (not prose/docstrings, which are
    free to *describe* the prohibition).
    """
    import ast

    source = (
        Path(__file__).resolve().parents[3]
        / "skills/beads/scripts/detect_write_conflicts.py"
    ).read_text()
    tree = ast.parse(source)
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    forbidden = {"line_range", "start_line", "end_line", "lineno", "line_ranges"}
    assert identifiers.isdisjoint(forbidden)
