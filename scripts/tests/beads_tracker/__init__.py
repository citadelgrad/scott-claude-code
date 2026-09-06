"""Marks beads_tracker as a package so its helpers get namespaced module names.

See ``../beads_front/__init__.py`` for the collision this prevents: two test
directories may each hold a private ``_common.py``, and only a package name
keeps them apart in ``sys.modules``.
"""
