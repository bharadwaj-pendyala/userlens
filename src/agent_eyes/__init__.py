"""agent-eyes: check that a CLI or web UI works the way a user uses it, then that it is good to use.

Project specifics live in eyes.json and in plugin files. A plugin is a Python
file with ``checks(result, project) -> list[Check]``.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .core import Check, ConfigError, Project, Result  # noqa: E402

__all__ = ["Check", "ConfigError", "Project", "Result", "__version__"]
