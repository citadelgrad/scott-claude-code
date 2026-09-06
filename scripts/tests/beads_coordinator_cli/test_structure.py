"""AC-T13-004: dispatcher-thinness and CLI structural contract checks.

These tests never construct a run or dispatch a real operation -- they only
inspect ``beads_coordinator.py``'s own static shape (its module-level import
list, its ``argparse`` wiring, and its status/exit-code lookup tables) and its
``--help``/argument-error exit-code surface. The goal is to prove the
dispatcher is what its own docstring claims: "This dispatcher registers
subcommands and sequences calls into the frozen modules ... It does not
reimplement any invariant those modules already enforce."  Handler-body
behavior (what each subcommand actually *does*) is covered by the other files
in this suite; this file only proves the wiring is thin and complete.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys

import pytest

from . import _common as common

bc = common.bc

# The exact stdlib names beads_coordinator.py's own header imports from, plus
# the frozen sibling modules it sequences calls into. Any other top-level
# import (a new stdlib module, and especially something like ``hashlib`` or
# ``subprocess`` that would let the dispatcher recompute or shell out on its
# own rather than delegating) should fail this test and prompt a deliberate
# scope decision, not a silent drift.
_ALLOWED_STDLIB = {
    "argparse",
    "functools",
    "json",
    "shutil",
    "sys",
    "datetime",
    "pathlib",
    "typing",
    "__future__",
}
_ALLOWED_FROZEN = {
    "beads_ownership",
    "coordinator_front",
    "coordinator_handoff",
    "coordinator_integration",
    "coordinator_state",
    "coordinator_tracker",
    "direct_operation",
    "operation_result",
    "protected_action",
    "reconcile_run",
}

# Every non-{status,recover,action} subcommand the parser registers, and the
# two ``action <sub>`` compound commands ``main()`` dispatches by hand.
_EXPECTED_DIRECT_SUBCOMMANDS = {
    "start-run",
    "freeze",
    "verify",
    "review",
    "review-combined",
    "build-candidate",
    "apply-candidate",
    "tracker-update",
    "gate",
    "handoff-launch",
    "handoff-accept",
    "finish",
    "cleanup",
}
_EXPECTED_TOP_LEVEL_SUBCOMMANDS = _EXPECTED_DIRECT_SUBCOMMANDS | {
    "status",
    "recover",
    "action",
}


def _module_ast() -> ast.Module:
    source = bc.__file__
    with open(source, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=source)


def test_module_only_imports_stdlib_and_frozen_siblings():
    """No new business-logic dependency (esp. hashlib/subprocess) sneaks in.

    ``shutil`` is allowed only because ``_handle_cleanup`` uses
    ``shutil.rmtree`` for the one documented deletion primitive no frozen
    module exposes (see the comment above ``_CLEANUP_FIELDS``) -- everything
    else must come from a frozen sibling.
    """
    tree = _module_ast()
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                seen.add(node.module.split(".")[0])
    unexpected = seen - _ALLOWED_STDLIB - _ALLOWED_FROZEN
    assert unexpected == set()
    # And the reverse: every frozen module the docstring/design claims to
    # sequence into is actually imported somewhere, not just aspirational.
    assert _ALLOWED_FROZEN <= seen


def test_dispatcher_never_computes_hashes_or_shells_out_itself():
    """Identity/verification math and process execution stay in frozen code.

    A dispatcher that starts calling ``hashlib`` or ``subprocess`` directly
    would be reimplementing an invariant (identity computation, command
    execution) that ``safe_bd``/``safe_output``/``direct_operation`` already
    own -- exactly what the module docstring says this file must not do.
    """
    tree = _module_ast()
    names_used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names_used.add(node.id)
        elif isinstance(node, ast.Attribute):
            pass
    assert "hashlib" not in names_used
    assert "subprocess" not in names_used


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    """Find a parser's registered ``_SubParsersAction``, if any.

    ``ArgumentParser._subparsers`` is typed ``_ArgumentGroup | None`` (it is
    ``None`` until ``add_subparsers()`` has been called), so an explicit
    ``assert ... is not None`` is required to narrow it for ``ty`` -- a
    ``# type: ignore`` comment has no effect on ``ty``'s analysis.
    """
    subparsers = parser._subparsers
    assert subparsers is not None
    return next(
        action
        for action in subparsers._group_actions
        if isinstance(action, argparse._SubParsersAction)
    )


def test_handlers_dict_matches_parser_direct_subcommands_exactly():
    """``_HANDLERS`` covers exactly the non-status/recover/action commands.

    No orphan subparser without a handler, and no stale handler entry for a
    subcommand the parser no longer registers.
    """
    parser = bc._parser()
    subparsers_action = _subparsers_action(parser)
    top_level = set(subparsers_action.choices.keys())
    assert top_level == _EXPECTED_TOP_LEVEL_SUBCOMMANDS
    assert set(bc._HANDLERS.keys()) == _EXPECTED_DIRECT_SUBCOMMANDS


def test_action_subcommand_registers_exactly_prepare_and_resolve():
    parser = bc._parser()
    subparsers_action = _subparsers_action(parser)
    action_parser = subparsers_action.choices["action"]
    action_sub_action = _subparsers_action(action_parser)
    assert set(action_sub_action.choices.keys()) == {"prepare", "resolve"}


def test_main_dispatch_covers_action_prepare_and_resolve_by_hand():
    """The compound ``action prepare``/``action resolve`` routing in
    ``main()`` is a literal dict built inline, not derived from
    ``_HANDLERS`` -- assert its two keys match the two real handler
    functions rather than re-deriving them from source text.
    """
    source = bc.__file__
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()
    assert '"action prepare": _handle_action_prepare' in text
    assert '"action resolve": _handle_action_resolve' in text


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [
        ("success", 0),
        ("human_action_required", 6),
        ("conflict", 4),
        ("inconclusive", 5),
        ("blocked", 1),
        ("failed", 1),
        ("totally-unknown-status", 1),
    ],
)
def test_exit_code_for_status_table(status, expected_exit):
    assert bc._exit_code_for_status(status) == expected_exit


def test_top_level_help_exits_zero_and_lists_every_subcommand(capsys):
    with pytest.raises(SystemExit) as excinfo:
        bc.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for name in _EXPECTED_TOP_LEVEL_SUBCOMMANDS:
        assert name in out


@pytest.mark.parametrize("subcommand", sorted(_EXPECTED_DIRECT_SUBCOMMANDS))
def test_each_subcommand_help_exits_zero(subcommand, capsys):
    with pytest.raises(SystemExit) as excinfo:
        bc.main([subcommand, "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert subcommand in out


def test_action_prepare_and_resolve_help_exit_zero(capsys):
    for args in (["action", "prepare", "--help"], ["action", "resolve", "--help"]):
        with pytest.raises(SystemExit) as excinfo:
            bc.main(args)
        assert excinfo.value.code == 0


def test_missing_subcommand_is_a_usage_error_not_a_crash(capsys):
    """``required=True`` on the top-level subparsers means invoking with no
    subcommand at all is argparse's own well-known usage-error exit (2), not
    an unhandled traceback -- proving the dispatcher's argparse wiring, not
    any handler body.
    """
    with pytest.raises(SystemExit) as excinfo:
        bc.main([])
    assert excinfo.value.code == 2


def test_unknown_subcommand_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        bc.main(["not-a-real-subcommand"])
    assert excinfo.value.code == 2


def test_cli_invoked_as_a_real_subprocess_reports_usage_error_for_no_args():
    """One genuine, unmocked subprocess invocation of the real CLI entry
    point, so the ``if __name__ == "__main__": raise SystemExit(main())``
    footer is itself exercised at least once, not just ``bc.main`` in-process.
    """
    completed = subprocess.run(
        [sys.executable, str(common.CLI)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 2
