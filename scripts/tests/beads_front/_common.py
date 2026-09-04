"""Shared dynamic-load helper for beads_front tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills/beads/scripts"

_counter = {"n": 0}


def load(name: str):
    _counter["n"] += 1
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        f"beads_front_{_counter['n']}_{name}", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
