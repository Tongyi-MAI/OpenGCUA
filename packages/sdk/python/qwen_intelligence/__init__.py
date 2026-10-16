"""Qwen-Intelligence SDK — public alias namespace.

The implementation lives in the internal package :mod:`mai_ui` (the compiled native
extension is built against that module path, so the internal name is preserved).
This namespace re-exports it so the frozen public import path
``qwen_intelligence`` works as documented.
"""
from mai_ui import *  # noqa: F401,F403
from mai_ui import Runtime, Session, Device  # noqa: F401

__all__ = ["Runtime", "Session", "Device"]
