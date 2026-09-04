"""RED-first tests for package_lane.py (scc-0pu.11 / t10)."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills/beads/scripts"
PACKET_TEST = ROOT / "scripts/tests/beads_contract/test_worker_packet.py"
MODULE = SCRIPTS / "package_lane.py"

_counter = {"n": 0}


def _member_bytes(tar: tarfile.TarFile, name: str) -> bytes:
    """Read one archive member, failing loudly when it has no payload.

    ``TarFile.extractfile`` returns None for directories and links, so the
    caller must prove the member it asked for is a regular file.
    """
    handle = tar.extractfile(name)
    assert handle is not None, f"archive member has no payload: {name}"
    with handle:
        return handle.read()


def _load(path: Path):
    _counter["n"] += 1
    spec = importlib.util.spec_from_file_location(
        f"beads_package_lane_{_counter['n']}_{path.stem}", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _factory(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        f"beads_package_factory_{tmp_path.name}", PACKET_TEST
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _identity(tmp_path: Path) -> dict:
    return {
        "run_id": "run-0123456789abcdef-20260903T120000.000000Z-ABCDEFGH",
        "issue_id": "scc-test",
        "attempt_id": "attempt-001",
        "ownership_epoch": 3,
        "worker_result_sha256": "c" * 64,
        "artifact_path": str(tmp_path / "lanes" / "key" / "lane-package.tar"),
    }


SCOPE = {
    "allowed_paths": ["src/**"],
    "forbidden_paths": [".beads/**"],
}


def _lane(tmp_path: Path):
    """Real repo lane: one tracked change plus untracked file and symlink."""
    pkg = _load(MODULE)
    factory = _factory(tmp_path)
    repo, lane, head = factory._git_lane(tmp_path)
    outbox = lane / "outbox"
    outbox.mkdir(parents=True)
    src = lane / "src"
    src.mkdir()
    (src / "code.py").write_text("print('lane')\n")
    (src / "extra.txt").write_text("notes\n")
    (src / "link").symlink_to("code.py")
    return pkg, repo, lane, head, outbox


def _build(pkg, lane, head, outbox, **overrides):
    kwargs = dict(
        outbox=outbox,
        scope=SCOPE,
        packet_budgets={},
        identity=_identity(lane.parents[2]),
    )
    kwargs.update(overrides)
    return pkg.build_lane_package(lane, head, **kwargs)


def test_build_is_deterministic_and_freeze_is_schema_valid(tmp_path: Path) -> None:
    pkg, repo, lane, head, outbox = _lane(tmp_path)

    first = _build(pkg, lane, head, outbox)
    second = _build(pkg, lane, head, outbox)

    assert first.archive_bytes == second.archive_bytes
    assert first.manifest == second.manifest
    assert first.candidate_tree_sha256 == second.candidate_tree_sha256
    assert first.archive_sha256 == second.archive_sha256

    freeze = first.lane_freeze
    assert freeze["schema_version"] == "beads.lane-freeze.v1"
    identity = _identity(tmp_path)
    assert freeze["run_id"] == identity["run_id"]
    assert freeze["worker_result_sha256"] == identity["worker_result_sha256"]
    assert freeze["transfer_mode"] == "patch_package"
    assert freeze["base_sha"] == head
    assert freeze["observed_head_sha"] == freeze["base_sha"]
    assert freeze["reproduction_status"] == "reproduced"
    assert freeze["packaging_tool_version"] == pkg.PACKAGING_TOOL_VERSION
    assert freeze["candidate_tree_sha256"] == first.candidate_tree_sha256
    kinds = {item["kind"] for item in freeze["inventory"]}
    assert kinds == {"file", "symlink"}
    by_path = {item["path"]: item for item in freeze["inventory"]}
    assert by_path["src/link"]["sha256"] == hashlib.sha256(b"code.py").hexdigest()
    # schema-validate the freeze record
    runtime = _load(SCRIPTS / "schema_runtime.py")
    assert not runtime.validate_instance("lane-freeze-v1.schema.json", freeze)


def test_manifest_is_written_last_and_partial_packages_fail(tmp_path: Path) -> None:
    pkg, repo, lane, head, outbox = _lane(tmp_path)
    built = _build(pkg, lane, head, outbox)

    with tarfile.open(fileobj=io.BytesIO(built.archive_bytes)) as tar:
        names = tar.getnames()
    assert names[-1] == "manifest.json"

    # A crash-truncated package never validates and never yields a manifest.
    with tarfile.open(fileobj=io.BytesIO(built.archive_bytes)) as tar:
        manifest_offset = tar.getmember("manifest.json").offset_data
    for cut in (0, 1, 64, 512, manifest_offset + 10):
        with pytest.raises(pkg.LanePackageError):
            pkg.verify_package(built.archive_bytes[:cut])
    # The manifest member never precedes any content member.
    with tarfile.open(fileobj=io.BytesIO(built.archive_bytes)) as tar:
        offset = tar.getmember("manifest.json").offset_data
    for name in names[:-1]:
        with tarfile.open(fileobj=io.BytesIO(built.archive_bytes)) as tar:
            assert tar.getmember(name).offset_data < offset


def test_typed_refusals_name_the_path(tmp_path: Path) -> None:
    pkg, repo, lane, head, outbox = _lane(tmp_path)

    # Scope escape: untracked file outside allowed scope.
    (lane / "evil.txt").write_text("outside\n")
    with pytest.raises(pkg.LanePackageError, match="evil.txt") as escape:
        _build(pkg, lane, head, outbox)
    assert escape.value.code == "LANE_SCOPE_ESCAPE"

    (lane / "evil.txt").unlink()
    # Forbidden path.
    (lane / ".beads").mkdir(parents=True, exist_ok=True)
    (lane / ".beads" / "x.json").write_text("{}")
    with pytest.raises(pkg.LanePackageError, match=".beads/x.json") as forbidden:
        _build(pkg, lane, head, outbox)
    assert forbidden.value.code == "LANE_FILE_FORBIDDEN"

    import shutil

    shutil.rmtree(lane / ".beads")
    # Oversized untracked file under a packet-declared budget.
    big = lane / "src" / "big.bin"
    big.write_bytes(b"0" * 300)
    with pytest.raises(pkg.LanePackageError, match="src/big.bin") as over:
        _build(
            pkg, lane, head, outbox, packet_budgets={"max_untracked_file_bytes": 100}
        )
    assert over.value.code.startswith("LANE_FILE_OVERSIZED")
    big.unlink()

    # Sensitive content in an untracked file.
    secret = lane / "src" / "key.pem"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    with pytest.raises(pkg.LanePackageError, match="src/key.pem") as sensitive:
        _build(pkg, lane, head, outbox)
    assert sensitive.value.code == "LANE_SENSITIVE_CONTENT"


def test_reproduction_detects_tampered_candidate(tmp_path: Path) -> None:
    pkg, repo, lane, head, outbox = _lane(tmp_path)
    built = _build(pkg, lane, head, outbox)

    into = tmp_path / "disposable"
    reproduced = pkg.reconstruct_candidate_tree(
        io.BytesIO(built.archive_bytes), into=into
    )
    assert reproduced["reproduction_status"] == "reproduced"
    assert reproduced["candidate_tree_sha256"] == built.candidate_tree_sha256
    assert (into / "src" / "code.py").read_text() == "print('lane')\n"
    assert (into / "src" / "link").is_symlink()
    assert stat.S_IMODE((into / "src" / "code.py").stat().st_mode) & 0o755

    # Forge the recorded candidate hash: the reconstructed tree differs from
    # the source lane by one byte and must not report "reproduced".
    with tarfile.open(fileobj=io.BytesIO(built.archive_bytes)) as tar:
        members = {
            name: (None if tar.getmember(name).issym() else _member_bytes(tar, name))
            for name in tar.getnames()
        }
        manifest = json.loads(_member_bytes(tar, "manifest.json"))
    manifest["candidate_tree_sha256"] = "0" * 64
    forged = members.copy()
    forged["manifest.json"] = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in forged.items():
            info = tarfile.TarInfo(name)
            info.mtime = 0
            if data is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "code.py"
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    tampered = pkg.reconstruct_candidate_tree(buffer, into=tmp_path / "disposable2")
    assert tampered["reproduction_status"] != "reproduced"


def test_archive_member_hash_drift_fails_verification(tmp_path: Path) -> None:
    pkg, repo, lane, head, outbox = _lane(tmp_path)
    built = _build(pkg, lane, head, outbox)

    with pytest.raises(pkg.LanePackageError):
        pkg.verify_package(
            built.archive_bytes.replace(b"print('lane')", b"print('LAME')")
        )


def test_empty_inventory_refused(tmp_path: Path) -> None:
    pkg, repo, lane, head, outbox = _lane(tmp_path)
    # Remove every change: no tracked diff, no untracked inventory.
    for child in (lane / "src").iterdir():
        child.unlink()
    (lane / "src").rmdir()
    with pytest.raises(pkg.LanePackageError, match="LANE_FREEZE_EMPTY_INVENTORY"):
        _build(pkg, lane, head, outbox)


def test_cli_builds_freeze_and_archive(tmp_path: Path) -> None:
    pkg, repo, lane, head, outbox = _lane(tmp_path)
    scope_file = tmp_path / "scope.json"
    scope_file.write_text(json.dumps(SCOPE))
    identity_file = tmp_path / "identity.json"
    identity_file.write_text(json.dumps(_identity(tmp_path)))
    dest = tmp_path / "dest"
    dest.mkdir()
    identity = json.loads(identity_file.read_text())
    identity["artifact_path"] = str(dest / "lane-package.tar")
    identity_file.write_text(json.dumps(identity))

    code = pkg.main(
        [
            "package-lane",
            "--worktree",
            str(lane),
            "--base-sha",
            head,
            "--outbox",
            str(outbox),
            "--scope-file",
            str(scope_file),
            "--identity-file",
            str(identity_file),
            "--dest-dir",
            str(dest),
        ]
    )
    assert code == 0
    archive = dest / "lane-package.tar"
    freeze_path = dest / "lane-freeze.json"
    assert archive.is_file() and freeze_path.is_file()
    freeze = json.loads(freeze_path.read_bytes())
    assert freeze["reproduction_status"] == "reproduced"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == freeze["artifact_sha256"]


def test_forged_manifest_traversal_paths_refused(tmp_path: Path) -> None:
    """Adversarial probe: a forged package manifest cannot smuggle lexical
    traversal paths into reconstruction or candidate-apply targets."""
    pkg, repo, lane, head, outbox = _lane(tmp_path)
    built = _build(pkg, lane, head, outbox)

    with tarfile.open(fileobj=io.BytesIO(built.archive_bytes)) as tar:
        members = {
            name: (None if tar.getmember(name).issym() else _member_bytes(tar, name))
            for name in tar.getnames()
        }
        manifest = json.loads(_member_bytes(tar, "manifest.json"))
    evil = "escape/../../pwned.txt"
    manifest["candidate_entries"].append(
        {
            "path": evil,
            "kind": "file",
            "mode": 0o600,
            "size_bytes": 3,
            "sha256": hashlib.sha256(b"pwn").hexdigest(),
        }
    )
    forged = dict(members)
    forged[f"blob/{evil}"] = b"pwn"
    forged["manifest.json"] = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in forged.items():
            info = tarfile.TarInfo(name)
            info.mtime = 0
            if data is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "code.py"
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    with pytest.raises(pkg.LanePackageError, match="LANE_PACKAGE_INVALID"):
        pkg.verify_package(buffer.getvalue())
    with pytest.raises(pkg.LanePackageError, match="LANE_PACKAGE_INVALID"):
        pkg.reconstruct_candidate_tree(buffer, into=tmp_path / "disposable3")
    assert not (tmp_path.parent / "pwned.txt").exists()
