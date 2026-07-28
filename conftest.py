"""Pytest bootstrap: ensure the project package is importable without an install."""
import os
import sys

_ROOT = os.path.dirname(__file__)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
