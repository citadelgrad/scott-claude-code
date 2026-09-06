"""Secret-redaction and Hermes-limitation fixtures for the solo lifecycle
(scc-0pu.15 / t14).

Covers AC-T14-005: controlled surfaces contain no raw secret sentinel or
digest, and the disclosed Hermes plugin-free pre-interception limitation is
release-blocking if observed.

Every assertion below exercises a REAL production redaction boundary — never
a ``FakeNative`` echo — because ``direct_operation.execute`` itself performs
no redaction; it trusts the transport to have already sanitized. The three
boundaries actually enforcing "no raw secret reaches a controlled surface"
are:

- ``safe_bd.decode_output`` — sanitizes every native JSON reply (an "issue"
  surface) before it ever reaches calling code.
- ``safe_output.sanitize_value`` — sanitizes captured command stdout/stderr
  (a "command" surface) before ``run_command`` persists it.
- ``operation_result.build_operation_result`` — sanitizes the rendered
  operation-result tree (an "error" surface) before it is written or shown.

Fixtures live under ``scripts/tests/fixtures/beads_solo/`` as standalone JSON
files (the issue's "Required deliverables: Secret-bearing issue/command/error
fixtures"), each carrying clearly-fake sentinel values of at least 8 UTF-8
bytes (below that ``sanitize_value`` rejects a configured secret as
``SENSITIVE_VALUE_AMBIGUOUS`` rather than silently accepting it).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import _common as common

m = common.modules()
safe_bd = m.safe_bd
safe_output = m.safe_output
operation_result = m.operation_result

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/beads_solo"


def _load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _issue_get_request(issue_id, repo):
    return safe_bd.SafeBdRequest("issue_get", {"issue_id": issue_id}, repo, None)


def test_secret_bearing_issue_reply_is_redacted_by_the_real_decoder(tmp_path):
    """A native ``issue_get`` reply (an "issue" surface) never leaks its
    embedded secrets through ``safe_bd.decode_output`` — neither the
    pattern-matched ones (``_LABEL``/``_TOKEN`` fire even with an empty
    ``SensitiveSet``) nor the opaque, configured one (redacted only once
    named via an explicit ``SensitiveSet``).
    """
    records = _load_fixture("secret-bearing-issue.json")
    raw = json.dumps(records)
    issue_id = records[0]["id"]
    configured_secret = records[0]["notes"]
    request = _issue_get_request(issue_id, tmp_path)

    # Pattern-matched secrets (a fake GitHub token, a "password: ..." label
    # pair) are redacted with zero configuration, because decode_output
    # unconditionally sanitizes every JSON reply.
    unconfigured = safe_bd.decode_output(request, raw)
    encoded = repr(unconfigured)
    assert "ghp_FAKE0123456789FAKE0123456789FAKE01" not in encoded
    assert "SoloFixtureP4ss!2026" not in encoded
    assert "[REDACTED]" in encoded
    # The opaque configured secret is not pattern-shaped, so it survives
    # until a caller actually names it as sensitive.
    assert configured_secret in encoded

    # Once named, the same real decoder redacts it too — proving the
    # boundary, not a stand-in.
    configured = safe_bd.decode_output(
        request, raw, sensitive=safe_output.SensitiveSet((configured_secret,))
    )
    full_encoded = repr(configured)
    assert configured_secret not in full_encoded
    assert "ghp_FAKE0123456789FAKE0123456789FAKE01" not in full_encoded
    assert "SoloFixtureP4ss!2026" not in full_encoded


def test_secret_bearing_command_output_is_redacted_before_persistence(tmp_path):
    """A captured command's stdout (a "command" surface) is sanitized by the
    same ``safe_output.sanitize_value`` function ``run_command`` calls on
    every real subprocess's captured output before it is written to a log or
    handed back to a caller.
    """
    payload = _load_fixture("secret-bearing-command.json")
    raw_stdout = payload["raw_stdout"]

    result = safe_output.sanitize_value(
        raw_stdout,
        sensitive=safe_output.SensitiveSet(()),
        max_utf8_bytes=safe_bd.MAX_NATIVE_BYTES,
    )

    assert result.status == "REDACTED"
    assert result.redaction_count >= 1
    assert "sk-fixture-FAKEBEARERTOKEN0123456789" not in result.value
    assert "[REDACTED]" in result.value
    assert result.error_code is None


def _base_operation_result(tmp_path):
    return {
        "schema_version": "beads.operation-result.v1",
        "operation": "observe",
        "status": "success",
        "request_id": "request-solo-t14-0005",
        "issue_ids": ["scc-solo-secret-1"],
        "root_issue_id": "scc-solo-secret-1",
        "workspace": {
            "repository_root": str(tmp_path),
            "workspace_sha256": "a" * 64,
            "cli_version": "1.2.2",
        },
        "observed_changes": [],
        "authority": {
            "requested": ["read"],
            "exercised": ["read"],
            "readback_proven": ["read"],
        },
        "native_events": [],
        "verification": {
            "required": False,
            "disposition": "not_required",
            "target_sha256": None,
            "observed_at": None,
            "evidence": [],
        },
        "creation": None,
        "pending_actions": [],
        "blockers": [],
        "warnings": [],
        "coverage_gaps": [],
        "errors": [],
        "error_code": None,
        "safe_next_action": None,
        "run_id": None,
        "checkpoint": None,
        "cancellation_reason": None,
    }


def test_secret_bearing_error_evidence_is_redacted_before_persistence(tmp_path):
    """An operation-result's error evidence (an "error" surface) never
    carries a raw secret sentinel once it passes through the real
    ``operation_result.build_operation_result`` — the same public entry
    point ``direct_operation``/CLI callers use to render a result.
    """
    fixture = _load_fixture("secret-bearing-error.json")
    sentinel = fixture["sensitive_value"]
    value = _base_operation_result(tmp_path)
    value.update(
        status="failed",
        error_code=fixture["errors"][0]["code"],
        safe_next_action={
            "code": "RETRY",
            "template_id": "retry",
            "field_path": "/",
            "parameters": [],
        },
        errors=fixture["errors"],
    )

    built = operation_result.build_operation_result(
        operation_result.OperationBuildInput(
            value, safe_output.SensitiveSet((sentinel,))
        )
    )

    assert sentinel not in repr(built.value)
    assert built.value["errors"][0]["parameters"] == ["[REDACTED]"]
    assert built.value["errors"][0]["code"] == "SOLO_SECRET_EXPOSURE_ATTEMPT"


def test_disclosed_hermes_pre_interception_limitation_is_release_blocking():
    """The plugin-free Hermes child-summary limitation is a named,
    disclosed, release-blocking constant — not something a caller could
    silently downgrade to advisory. Cross-checked (read-only) against the
    frozen ``beads_contract`` limitations fixture, never duplicated or
    edited.
    """
    assert (
        operation_result.PRE_MODEL_CHILD_OUTPUT_LIMITATION
        == "UNPREVENTABLE_PRE_INTERCEPTION_LEAK"
    )
    root = Path(__file__).resolve().parents[3]
    limitations = json.loads(
        (
            root
            / "scripts/tests/fixtures/beads_contract/release-blocking-limitations.json"
        ).read_text()
    )
    plugin_free = limitations["plugin_free_child_summary"]
    assert (
        plugin_free["classification"]
        == operation_result.PRE_MODEL_CHILD_OUTPUT_LIMITATION
    )
    assert plugin_free["release_blocking"] is True
    assert plugin_free["prevented_before_parent_model_context"] is False
    assert plugin_free["controlled_persistence_rejects_or_redacts"] is True
