"""agent-eyes: let an agent see a CLI or web UI the way a user does, then tell it what to fix.

Project specifics live in eyes.json and in plugin files. A plugin is a Python
file with ``checks(result, project) -> list[Check]``.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .core import Check, ConfigError, Project, Result  # noqa: E402

__all__ = ["Check", "ConfigError", "Project", "Result", "__version__"]
