from __future__ import annotations

from pathlib import Path

_pkg_root = Path(__file__).resolve().parent
_main_pkg = _pkg_root.parent / "main"
__path__ = [str(_main_pkg)]

from .app import DeepEMApp, build_app, build_app_from_env

__all__ = ["DeepEMApp", "build_app", "build_app_from_env"]
