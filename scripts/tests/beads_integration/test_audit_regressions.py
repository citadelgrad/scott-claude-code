"""RED-first regression tests for the scc-0pu.11 runtime audit (a254b42).

Each test reproduces one audit witness against real repositories and real
run directories, exactly as the audit probes did.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from . import _common as common

RUN_ID = "run-0123456789abcdef-20260904T020000.000000Z-AAAAAAAD"


def _clean_repo(factory, tmp_path: Path):
    repo, lane_worktree, head = factory._git_lane(tmp_path)
    (repo / ".gitignore").write_text(
        ".hermes/\nlane/\nlane-*/\nsensitive-values.json\nsnapshot.json\n"
    )
    common.run_git(repo, "add", ".gitignore")
    common.run_git(repo, "commit", "-m", "ignore run artifacts")
    common.run_git(lane_worktree, "merge", "--ff-only", "main")
    head = common.run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    return repo, lane_worktree, head


def _add_worktree(repo: Path, index: int) -> Path:
    worktree = repo / f"lane-{index}"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", f"lane-{index}"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return worktree


def _lane_fixture(
    tmp_path: Path,
    lanes: list[dict],
    *,
    stop_before_freeze: bool = False,
):
    """lanes: [{issue_id, file, content, allowed, mode?}] -> imported attempts.

    Unless stop_before_freeze, every lane is frozen and verified like the
    deployed verify_lane flow.
    """
    m = common.modules()
    factory = common.factory()
    repo, lane_worktree, head = _clean_repo(factory, tmp_path)
    run = common.make_run(m, repo, RUN_ID, [item["issue_id"] for item in lanes])
    results: dict[str, dict] = {}
    for index, spec in enumerate(lanes):
        worktree = lane_worktree if index == 0 else _add_worktree(repo, index)
        outbox = worktree / "outbox"
        epoch = run["epochs"][spec["issue_id"]]
        packet = common.lane_packet(
            factory,
            tmp_path,
            issue_id=spec["issue_id"],
            run_id=RUN_ID,
            worktree=worktree,
            outbox=outbox,
            head=head,
            allowed=spec.get("allowed", ["src/**"]),
        )
        mode = spec.get("mode", "patch_package")
        if mode != packet["scope"]["integration_mode"]:
            packet["scope"]["integration_mode"] = mode
            packet["scope"]["local_commit"] = mode == "commit"
            packet["scope"]["external_io"] = mode == "external_export"
        if mode == "commit":
            digest, result_sha = _import_commit_attempt(
                m,
                run["run_directory"],
                issue_id=spec["issue_id"],
                epoch=epoch,
                worktree=worktree,
                outbox=outbox,
                head=head,
                changed_file=spec["file"],
                file_content=spec["content"],
                packet=packet,
            )
        else:
            digest, result_sha = common.import_attempt(
                m,
                run["run_directory"],
                issue_id=spec["issue_id"],
                epoch=epoch,
                worktree=worktree,
                outbox=outbox,
                head=head,
                changed_file=spec["file"],
                file_content=spec["content"],
                packet=packet,
            )
        entry = common.issue_entry(
            m, epoch=epoch, result_sha=result_sha, packet_sha=digest
        )
        results[spec["issue_id"]] = {
            "entry": entry,
            "epoch": epoch,
            "result_sha": result_sha,
            "packet": packet,
            "worktree": worktree,
            "outbox": outbox,
        }
    run["finish"]({issue_id: item["entry"] for issue_id, item in results.items()})
    context = m.coordinator_integration.open_run(run["run_directory"])
    fixture = {
        "m": m,
        "factory": factory,
        "repo": repo,
        "head": head,
        "run": run,
        "run_directory": run["run_directory"],
        "context": context,
        "results": results,
        "freeze_shas": {},
    }
    if stop_before_freeze:
        return fixture
    for issue_id, item in results.items():
        frozen = m.coordinator_integration.freeze_lane(
            context, issue_id, expected_result_sha256=item["result_sha"]
        )
        assert frozen["status"] == "success", frozen
        verified = m.coordinator_integration.verify_lane(context, issue_id)
        assert verified["status"] in {"success", "partial"}, verified
        if verified["status"] == "partial":
            review = common.reviewer_record(
                run_id=RUN_ID,
                target_kind="lane_freeze",
                target_sha256=frozen["freeze_sha256"],
                reviewer_id=f"reviewer-{issue_id}",
                implementer_ids=[f"implementer-{issue_id}"],
            )
            path = common.write_review(m, fixture["run_directory"], review)
            outcome = m.coordinator_integration.record_review(context, issue_id, path)
            assert outcome["status"] == "success"
        fixture["freeze_shas"][issue_id] = frozen["freeze_sha256"]
    return fixture


def _import_commit_attempt(
    m,
    run_directory: Path,
    *,
    issue_id: str,
    epoch: int,
    worktree: Path,
    outbox: Path,
    head: str,
    changed_file: str,
    file_content: str,
    packet: dict,
) -> tuple[str, str]:
    """Honest commit-mode worker attempt: commit the scoped change, freeze it."""
    raw = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    previous_cwd = os.getcwd()
    os.chdir(worktree)
    monkey_context = m.worker_result.initialize_attempt(
        raw, expected_packet_sha256=digest, ownership_epoch=epoch
    )
    evidence = m.worker_result.run_declared_command(
        monkey_context,
        command_index=0,
        sensitive_values_file=common.descriptor(worktree),
    )
    target = worktree / changed_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(file_content)
    common.run_git(worktree, "add", changed_file)
    common.run_git(worktree, "commit", "-q", "-m", "lane work")
    lane_head = common.run_git(worktree, "rev-parse", "HEAD").stdout.decode().strip()
    lane_tree = (
        common.run_git(worktree, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
    )
    snapshot = m.lane_snapshot.capture(worktree, head, exclude=outbox)
    frozen_descriptor = json.dumps(
        {"identity": lane_head, "tree_sha": lane_tree, "type": "commit"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    record = outbox / "command-000.json"
    stdout = Path(evidence["safe_result"]["stdout_log"]["path"])
    stderr = Path(evidence["safe_result"]["stderr_log"]["path"])

    def identity(path: Path, artifact_type: str) -> dict:
        data = path.read_bytes()
        return {
            "type": artifact_type,
            "path": str(path),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    candidate = {
        "schema_version": "beads.worker-execution-result.v1",
        "run_id": packet["run_id"],
        "attempt_id": packet["attempt_id"],
        "issue_id": packet["issue"]["id"],
        "packet_sha256": digest,
        "status": "completed",
        "repository": {
            "worktree": str(worktree),
            "branch": packet["repository"]["branch"],
            "base_sha": head,
            "head_sha": snapshot.head_sha,
        },
        "lane_state": snapshot.lane_state,
        "changes": {
            "paths": list(snapshot.changed_paths),
            "outside_allowed_scope": [],
        },
        "verification": [
            {
                "command": json.dumps(evidence["argv"], separators=(",", ":")),
                "exit_code": evidence["safe_result"]["exit_code"],
                "started_at": evidence["safe_result"]["started_at"],
                "finished_at": evidence["safe_result"]["finished_at"],
                "log_path": str(record),
                "log_sha256": hashlib.sha256(record.read_bytes()).hexdigest(),
            }
        ],
        "acceptance_evidence": [
            {
                "acceptance_id": packet["issue"]["acceptance_ids"][0],
                "status": "supported",
                "evidence_paths": [str(record)],
            }
        ],
        "artifacts": [
            identity(record, "command_evidence"),
            identity(stdout, "stdout_log"),
            identity(stderr, "stderr_log"),
        ],
        "blockers": [],
        "errors": [],
        "cancellation_reason": None,
        "skipped_checks": [],
        "residual_risks": [],
        "summary": "scoped work committed",
        "integration_mode": "commit",
        "worker_frozen_artifact": {
            "type": "commit",
            "identity": lane_head,
            "path": None,
            "sha256": hashlib.sha256(frozen_descriptor).hexdigest(),
            "tree_sha": lane_tree,
        },
    }
    m.worker_result.finalize_attempt(
        monkey_context, candidate, sensitive_values_file=common.descriptor(worktree)
    )
    try:
        if run_directory is not None:
            imports = run_directory / "imports" / common.issue_key(m, issue_id)
            m.coordinator_state.ensure_owner_directory(imports, root=run_directory)
            for name, data in (
                ("packet.json", raw),
                ("result.json", (outbox / "result.json").read_bytes()),
                ("receipt.json", (outbox / "receipt.json").read_bytes()),
            ):
                target_file = imports / name
                target_file.write_bytes(data)
                target_file.chmod(0o600)
    finally:
        os.chdir(previous_cwd)
    return digest, hashlib.sha256((outbox / "result.json").read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# P1-1: freeze must bind to the validated lane (TOCTOU smuggling)
# ---------------------------------------------------------------------------


def test_p1_1_freeze_rejects_content_drift_between_validation_and_build(
    tmp_path: Path, monkeypatch
) -> None:
    """Audit witness 1: seed.txt mutated to SMUGGLED during the builder's
    internal capture used to land in the hash-pinned package while verify_lane
    still returned success.  The freeze must fail closed with a typed error."""
    fixture = _lane_fixture(
        tmp_path,
        [
            {
                "issue_id": "scc-a",
                "file": "src/alpha.py",
                "content": "a = 1\n",
                "allowed": ["src/**", "seed.txt"],
            }
        ],
        stop_before_freeze=True,
    )
    m = fixture["m"]
    context = fixture["context"]
    item = fixture["results"]["scc-a"]
    real_capture = m.coordinator_integration.lane_snapshot.capture
    calls = {"n": 0}

    def hooked(worktree, base_sha, **kwargs):
        calls["n"] += 1
        # capture #1: result validation; #2: _recapture; #3: builder capture.
        if calls["n"] == 3:
            (Path(worktree) / "seed.txt").write_text("SMUGGLED = 1\n")
        return real_capture(worktree, base_sha, **kwargs)

    monkeypatch.setattr(m.coordinator_integration.lane_snapshot, "capture", hooked)
    with pytest.raises(m.coordinator_integration.IntegrationError) as refused:
        m.coordinator_integration.freeze_lane(
            context, "scc-a", expected_result_sha256=item["result_sha"]
        )
    assert refused.value.code in {
        "LANE_FREEZE_LANE_DRIFT",
        "LANE_FREEZE_UNREPRODUCIBLE",
    }
    # Fail closed: no freeze record and no package were written.
    lanes_dir = fixture["run_directory"] / "lanes" / common.issue_key(m, "scc-a")
    assert not (lanes_dir / "lane-freeze.json").exists()
    assert not (lanes_dir / "lane-package.tar").exists()
    # The lane itself was left in the smuggled state; nothing was frozen.


# ---------------------------------------------------------------------------
# P1-2: tracked-change lanes must integrate (no double-apply)
# ---------------------------------------------------------------------------


def test_p1_2_tracked_modification_lane_end_to_end(tmp_path: Path) -> None:
    """Audit witness 2: an honest tracked-modification lane must freeze,
    verify, build, and apply successfully with the exact content."""
    fixture = _lane_fixture(
        tmp_path,
        [
            {
                "issue_id": "scc-a",
                "file": "seed.txt",
                "content": "seed = 'lane-value'\n",
                "allowed": ["seed.txt"],
            }
        ],
    )
    m = fixture["m"]
    context = fixture["context"]
    built = m.coordinator_integration.build_candidate(
        context, lane_freeze_sha256s=list(fixture["freeze_shas"].values())
    )
    assert built["status"] == "success", built
    applied = m.coordinator_integration.apply_candidate(
        context, built["candidate_id"], expected_predecessor=fixture["head"]
    )
    assert applied["status"] == "success", applied
    assert (fixture["repo"] / "seed.txt").read_text() == "seed = 'lane-value'\n"
    status = m.coordinator_integration._git(
        fixture["repo"], "status", "--porcelain"
    ).decode()
    assert status.strip() == ""


# ---------------------------------------------------------------------------
# P1-3: crash between update-ref and reset --hard must recover
# ---------------------------------------------------------------------------


def _journal_events(m, run_directory: Path) -> list[dict]:
    records = (run_directory / "operations.jsonl").read_text().strip().splitlines()
    return [json.loads(line) for line in records]


def test_p1_3_apply_crash_between_update_ref_and_reset_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    """Audit witness 3: a real crash in the window between update-ref and
    reset --hard left the primary with a moved HEAD and stale worktree; the
    retry then escaped a raw schema failure and wedged forever.  Recovery
    must emit schema-valid APPLIED evidence and complete materialization."""
    fixture = _lane_fixture(
        tmp_path,
        [{"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"}],
    )
    m = fixture["m"]
    context = fixture["context"]
    built = m.coordinator_integration.build_candidate(
        context, lane_freeze_sha256s=list(fixture["freeze_shas"].values())
    )
    assert built["status"] == "success", built
    real_run = m.coordinator_integration.subprocess.run

    def crashing_run(*args, **kwargs):
        completed = real_run(*args, **kwargs)
        argv = list(args[0]) if args else list(kwargs.get("args", ()))
        if argv[:2] == ["git", "update-ref"]:
            # Hard crash exactly between update-ref and reset --hard.
            raise SystemExit(9)
        return completed

    monkeypatch.setattr(m.coordinator_integration.subprocess, "run", crashing_run)
    with pytest.raises(SystemExit):
        m.coordinator_integration.apply_candidate(
            context, built["candidate_id"], expected_predecessor=fixture["head"]
        )
    # The crash window state: HEAD moved, worktree stale.
    repository = fixture["repo"]
    head_now = common.run_git(repository, "rev-parse", "HEAD").stdout.decode().strip()
    assert head_now != fixture["head"]
    stale = common.run_git(repository, "status", "--porcelain")
    assert stale.stdout.strip() != b""
    assert not (repository / "src" / "alpha.py").exists()

    # Retry: recovery must classify, complete materialization, and emit
    # schema-valid APPLIED evidence — no raw schema failure, no wedge.
    context2 = m.coordinator_integration.open_run(fixture["run_directory"])
    replay = m.coordinator_integration.apply_candidate(
        context2, built["candidate_id"], expected_predecessor=fixture["head"]
    )
    assert replay["status"] == "success", replay
    assert (repository / "src" / "alpha.py").read_text() == "a = 1\n"
    status = common.run_git(repository, "status", "--porcelain")
    assert status.stdout.strip() == b""
    events = _journal_events(m, fixture["run_directory"])
    resolution = [e for e in events if e["phase"] == "RESOLUTION"][-1]
    assert resolution["status"] == "APPLIED"
    assert resolution["effect_type"] == "PRIMARY_INTEGRATION_PREPARED"
    assert isinstance(resolution["readback_evidence_path"], str)
    assert Path(resolution["readback_evidence_path"]).is_file()
    assert (
        resolution["readback_evidence_sha256"]
        == hashlib.sha256(
            Path(resolution["readback_evidence_path"]).read_bytes()
        ).hexdigest()
    )
    findings = m.schema_runtime.validate_instance(
        "operation-journal-event-v1.schema.json", resolution
    )
    assert not findings


# ---------------------------------------------------------------------------
# P2-4: crash mid-build_candidate must be retryable
# ---------------------------------------------------------------------------


def _candidate_id(m, predecessor: str, freeze_shas: list[str]) -> str:
    ordered = sorted(set(freeze_shas))
    raw = (
        json.dumps(
            {"lanes": ordered, "predecessor": predecessor},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        + b"\n"
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def test_p2_4_crash_mid_build_candidate_is_retryable(
    tmp_path: Path, monkeypatch
) -> None:
    """Audit witness 4: a crash after `git worktree add` (before
    candidate.json) permanently wedged the deterministic candidate path with
    CANDIDATE_TREE_EXISTS.  The retry must clean up and rebuild."""
    fixture = _lane_fixture(
        tmp_path,
        [{"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"}],
    )
    m = fixture["m"]
    shas = list(fixture["freeze_shas"].values())

    def crashed(tree_dir):
        raise SystemExit(7)

    monkeypatch.setattr(m.coordinator_integration, "_write_tree", crashed)
    with pytest.raises(SystemExit):
        m.coordinator_integration.build_candidate(
            context=fixture["context"], lane_freeze_sha256s=shas
        )

    candidate_id = _candidate_id(m, fixture["head"], shas)
    tree_dir = fixture["run_directory"] / "candidates" / candidate_id / "tree"
    assert tree_dir.exists()

    # Retry with a fresh context: must clean up the leftover tree and rebuild.
    monkeypatch.undo()
    context2 = m.coordinator_integration.open_run(fixture["run_directory"])
    rebuilt = m.coordinator_integration.build_candidate(
        context2, lane_freeze_sha256s=shas
    )
    assert rebuilt["status"] == "success", rebuilt
    assert rebuilt["candidate_id"] == candidate_id


# ---------------------------------------------------------------------------
# P2-5: commit-mode and external_export lanes in candidates
# ---------------------------------------------------------------------------


def test_p2_5_commit_mode_lane_builds_and_applies(tmp_path: Path) -> None:
    """Audit witness 5: a commit-mode lane froze fine but build_candidate
    escaped an untyped package_lane.LanePackageError from verify_package on
    the non-tar commit artifact.  A single commit-mode lane must build and
    apply through the cherry-pick path."""
    fixture = _lane_fixture(
        tmp_path,
        [
            {
                "issue_id": "scc-a",
                "file": "src/alpha.py",
                "content": "a = 1\n",
                "mode": "commit",
            }
        ],
    )
    m = fixture["m"]
    context = fixture["context"]
    built = m.coordinator_integration.build_candidate(
        context, lane_freeze_sha256s=list(fixture["freeze_shas"].values())
    )
    assert built["status"] == "success", built
    applied = m.coordinator_integration.apply_candidate(
        context, built["candidate_id"], expected_predecessor=fixture["head"]
    )
    assert applied["status"] == "success", applied
    assert (fixture["repo"] / "src" / "alpha.py").read_text() == "a = 1\n"
    status = m.coordinator_integration._git(
        fixture["repo"], "status", "--porcelain"
    ).decode()
    assert status.strip() == ""


def test_p2_5_mixed_mode_candidate_refused_typed(tmp_path: Path) -> None:
    """Combined candidates mixing transfer modes are honestly refused with a
    typed code, not an untyped crash."""
    fixture = _lane_fixture(
        tmp_path,
        [
            {"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"},
            {
                "issue_id": "scc-b",
                "file": "src/beta.py",
                "content": "b = 2\n",
                "mode": "commit",
            },
        ],
    )
    m = fixture["m"]
    with pytest.raises(m.coordinator_integration.IntegrationError) as refused:
        m.coordinator_integration.build_candidate(
            fixture["context"],
            lane_freeze_sha256s=list(fixture["freeze_shas"].values()),
        )
    assert refused.value.code == "CANDIDATE_MIXED_MODES"


def test_p2_5_external_export_candidate_refused_typed(tmp_path: Path) -> None:
    """external_export lanes are honestly refused in candidates with a typed
    code (their worker-built export tars are not package_lane packages)."""
    fixture = _lane_fixture(
        tmp_path,
        [{"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"}],
    )
    m = fixture["m"]
    # Freeze record bytes for an external_export lane (schema-valid, honest
    # identity, artifact pointing at a worker-built tar).
    lane_dir = fixture["run_directory"] / "lanes" / common.issue_key(m, "scc-a")
    freeze = json.loads((lane_dir / "lane-freeze.json").read_bytes())
    export_tar = lane_dir / "worker-export.tar"
    export_tar.write_bytes(b"worker-built-export\n")
    forged = {
        **freeze,
        "transfer_mode": "external_export",
        "artifact_path": str(export_tar),
        "artifact_sha256": hashlib.sha256(export_tar.read_bytes()).hexdigest(),
        "reproduction_status": "verified_identity",
    }
    raw = json.dumps(forged, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    (lane_dir / "lane-freeze.json").write_bytes(raw)
    export_freeze_sha = hashlib.sha256(raw).hexdigest()
    tree_dir = tmp_path / "candidate-tree"
    with pytest.raises(m.coordinator_integration.IntegrationError) as refused:
        m.coordinator_integration._apply_lanes(
            fixture["context"], tree_dir, [export_freeze_sha]
        )
    assert refused.value.code == "CANDIDATE_EXTERNAL_EXPORT_UNSUPPORTED"


# ---------------------------------------------------------------------------
# P2-6: offline validate_lane_freeze must reject cross-lane artifact swap
# ---------------------------------------------------------------------------


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def test_p2_6_offline_cross_lane_artifact_swap_rejected(tmp_path: Path) -> None:
    """Audit witness 6: freeze for lane A carrying lane B's package + B's
    tree/inventory validated ACCEPTED offline.  Package identity must bind
    to lane identity so the verbatim swap is refused."""
    fixture = _lane_fixture(
        tmp_path,
        [
            {"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"},
            {"issue_id": "scc-b", "file": "src/beta.py", "content": "b = 2\n"},
        ],
    )
    m = fixture["m"]
    context = fixture["context"]
    current = context.checkpoints.current(rebuild_pointer=True)
    entry_a = current.value["issues"]["scc-a"]
    _imports_dir_a, imports_a = m.coordinator_integration._load_imports(
        context, "scc-a"
    )
    packet_a, validated_a = m.coordinator_integration._validate_attempt(
        context, "scc-a", imports_a, entry_a, entry_a["ownership"]["epoch"]
    )
    lane_a_dir = fixture["run_directory"] / "lanes" / common.issue_key(m, "scc-a")
    lane_b_dir = fixture["run_directory"] / "lanes" / common.issue_key(m, "scc-b")
    freeze_a = json.loads((lane_a_dir / "lane-freeze.json").read_bytes())
    freeze_b = json.loads((lane_b_dir / "lane-freeze.json").read_bytes())
    swapped = {
        **freeze_a,
        "artifact_path": freeze_b["artifact_path"],
        "artifact_sha256": freeze_b["artifact_sha256"],
        "candidate_tree_sha256": freeze_b["candidate_tree_sha256"],
        "inventory": freeze_b["inventory"],
    }
    vlf = m.validate_lane_freeze
    with pytest.raises(vlf.LaneFreezeError) as refused:
        vlf.validate_lane_freeze(
            _canonical(swapped),
            result=validated_a,
            packet=packet_a,
            ownership_epoch=entry_a["ownership"]["epoch"],
            worktree_live=False,
        )
    assert refused.value.code == "LANE_FREEZE_IDENTITY_MISMATCH"
    # The honest offline freeze still validates.
    honest = vlf.validate_lane_freeze(
        _canonical(freeze_a),
        result=validated_a,
        packet=packet_a,
        ownership_epoch=entry_a["ownership"]["epoch"],
        worktree_live=False,
    )
    assert honest.value["issue_id"] == "scc-a"


# ---------------------------------------------------------------------------
# P3: packet worker_outbox inside allowed_paths; candidate-test packet hash
# ---------------------------------------------------------------------------


def test_p3_packet_outbox_inside_allowed_paths_refused(tmp_path: Path) -> None:
    """A packet whose worker_outbox lies inside allowed_paths silently
    excluded deliverables from inventory/freeze/review; it must be refused
    at validation with a typed code."""
    m = common.modules()
    factory = common.factory()
    repo, lane_worktree, head = factory._git_lane(tmp_path)
    outbox = lane_worktree / "outbox"
    packet = common.lane_packet(
        factory,
        tmp_path,
        issue_id="scc-a",
        run_id=RUN_ID,
        worktree=lane_worktree,
        outbox=outbox,
        head=head,
        allowed=["src/**"],
    )
    packet["scope"]["allowed_paths"] = ["src/**", "outbox/**"]
    raw = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(m.validate_worker_packet.PacketValidationError) as refused:
        m.validate_worker_packet.validate_packet(raw)
    assert "OUTBOX_INSIDE_SCOPE" in str(refused.value)


def test_p3_candidate_tests_bind_entry_packet_hash(tmp_path: Path) -> None:
    """_run_candidate_tests must revalidate the imported packet against the
    entry's packet_sha256, not trust run-dir bytes."""
    fixture = _lane_fixture(
        tmp_path,
        [{"issue_id": "scc-a", "file": "src/alpha.py", "content": "a = 1\n"}],
    )
    m = fixture["m"]
    imports = (
        fixture["run_directory"]
        / "imports"
        / common.issue_key(m, "scc-a")
        / "packet.json"
    )
    packet = json.loads(imports.read_bytes())
    packet["verification"]["required_commands"] = [["/usr/bin/true"]]
    imports.write_bytes(
        json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    )
    with pytest.raises(m.coordinator_integration.IntegrationError) as refused:
        m.coordinator_integration.build_candidate(
            fixture["context"],
            lane_freeze_sha256s=list(fixture["freeze_shas"].values()),
        )
    assert refused.value.code == "CANDIDATE_PACKET_INVALID"
    # Primary untouched.
    head = common.run_git(fixture["repo"], "rev-parse", "HEAD").stdout.decode().strip()
    assert head == fixture["head"]
    current = fixture["context"].checkpoints.current(rebuild_pointer=True)
    assert current.value["issues"]["scc-a"]["integration"]["state"] == "failed"
