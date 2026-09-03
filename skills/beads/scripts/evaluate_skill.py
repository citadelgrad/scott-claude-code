#!/usr/bin/env python3
"""Offline entry point for the frozen Beads skill evaluator."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_IMPL = _REPO / "evaluation/beads-skill/harness/evaluator.py"
_SPEC = importlib.util.spec_from_file_location("beads_skill_evaluator", _IMPL)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("cannot load Beads evaluation harness")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

EvaluationError = _MODULE.EvaluationError
aggregate_results = _MODULE.aggregate_results
check_thresholds = _MODULE.check_thresholds
compare_pair = _MODULE.compare_pair
digest = _MODULE.digest
normalize_trajectory = _MODULE.normalize_trajectory
prepare_pair = _MODULE.prepare_pair
run_treatment_process = _MODULE.run_treatment_process
scan_contamination = _MODULE.scan_contamination
score_run = _MODULE.score_run
validate_corpus = _MODULE.validate_corpus
validate_semantic_judgment = _MODULE.validate_semantic_judgment
render_markdown = _MODULE.render_markdown
main = _MODULE.main

if __name__ == "__main__":
    raise SystemExit(main())
