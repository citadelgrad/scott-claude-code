"""Marks beads_dag as a package so its helpers get namespaced module names.

Without this file, ``_common.py`` here and ``_common.py`` in a sibling test
directory both register under the single top-level ``sys.modules`` key
``_common``. Whichever suite pytest collects first wins, and the other silently
receives the wrong module -- an order-dependent failure that disappears when
either file is run on its own. As a package, this one is
``beads_dag._common``, which cannot collide.
"""
