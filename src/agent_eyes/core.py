"""Config, case and result types shared by every part of eyes."""

from __future__ import annotations

import difflib
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_VERSION = 1
CONFIG_CANDIDATES = ["eyes.json", "tests/ux/eyes.json", "tests/eyes/eyes.json", "eyes/eyes.json"]
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
DEFAULT_NOUNS = ["sentence", "file", "finding", "adverb", "error", "warning", "phrase", "issue", "line"]
CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

PROJECT_KEYS = {
    "version", "command", "cwd", "env", "timeout", "artifacts", "snapshots", "plugins",
    "sources", "plural_nouns", "titled", "services", "cases", "pass_env",
}
CASE_KEYS = {
    "id", "note", "argv", "command", "exit", "tty", "cols", "stdin", "stdin_file", "env", "cwd",
    "timeout", "workspace", "setup", "services", "page", "pass_env",
    "stdout_contains", "stdout_absent", "stdout_regex", "stdout_empty",
    "stderr_contains", "stderr_absent", "stderr_regex",
    "expect_ansi", "check_width", "json", "html", "html_contains",
}
SERVICE_KEYS = {"command", "cwd", "env", "ready", "timeout"}
PAGE_KEYS = {"steps", "viewport", "step_timeout", "allow"}
STEP_ACTIONS = {"goto", "click", "select_text", "fill", "press", "wait_for", "expect_text",
                "expect_count", "reload", "wait"}
STEP_OPTIONS = {"count", "text", "on", "state", "in"}


class ConfigError(Exception):
    pass


@dataclass
class Result:
    case: dict
    argv: list[str]
    cwd: Path
    out_dir: Path
    exit: int
    stdout_raw: str
    stderr: str
    tty: bool
    cols: int
    seconds: float
    harness_error: str = ""
    page_text: str = ""
    steps: list[dict] = field(default_factory=list)

    @property
    def stdout(self) -> str:
        return ANSI.sub("", self.stdout_raw)

    @property
    def is_page(self) -> bool:
        return "page" in self.case


@dataclass
class Check:
    check: str
    status: str
    detail: str = ""
    expected: str | None = None
    actual: str | None = None
    source: str | None = None
    extra: dict = field(default_factory=dict)


class Project:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.base = self.config_path.parent
        try:
            self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ConfigError(f"cannot read {config_path}: {error}") from None
        _validate(self.config)
        self.root = _find_root(self.base)
        self.artifacts = Path(self.expand(self.config.get("artifacts", "{root}/artifacts/eyes")))
        self.snapshots = self.base / self.config.get("snapshots", "snapshots")
        self.sources = self.config.get("sources", {})
        self.nouns = self.config.get("plural_nouns", DEFAULT_NOUNS)
        self.timeout = self.config.get("timeout", 30)
        self.services = self.config.get("services", {})
        self.plugins = [_load_plugin(self.base / p) for p in self.config.get("plugins", [])]

    def expand(self, value: str, out_dir: Path | None = None, workspace: Path | None = None) -> str:
        return (
            value.replace("{python}", sys.executable)
            .replace("{root}", str(self.root))
            .replace("{base}", str(self.base))
            .replace("{out}", str(out_dir or ""))
            .replace("{workspace}", str(workspace or ""))
        )

    def cases(self, only: list[str] | None = None) -> list[dict]:
        cases = self.config["cases"]
        if not only:
            return cases
        unknown = set(only) - {c["id"] for c in cases}
        if unknown:
            raise ConfigError(f"unknown case(s): {', '.join(sorted(unknown))}")
        return [c for c in cases if c["id"] in only]


def _validate(config: dict) -> None:
    if not isinstance(config, dict):
        raise ConfigError("the config must be a JSON object")
    version = config.get("version")
    if version != CONFIG_VERSION:
        raise ConfigError(f"set \"version\": {CONFIG_VERSION} at the top of the config (found {version!r})")
    _unknown_keys(config, PROJECT_KEYS, "config")
    for name, service in config.get("services", {}).items():
        _unknown_keys(service, SERVICE_KEYS, f"service {name!r}")
        if "command" not in service:
            raise ConfigError(f"service {name!r} needs a command")
    if "command" not in config and any("page" not in c and "command" not in c for c in config.get("cases", [])):
        raise ConfigError("set a top-level \"command\", or a \"command\" on each command case")

    seen = set()
    for case in config.get("cases", []):
        case_id = case.get("id", "")
        if not CASE_ID.fullmatch(case_id):
            raise ConfigError(f"case id {case_id!r} must match {CASE_ID.pattern} (it names a folder)")
        if case_id in seen:
            raise ConfigError(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        _unknown_keys(case, CASE_KEYS, f"case {case_id!r}")
        if "page" in case:
            _validate_page(case["page"], case_id)
        for name in case.get("services", []):
            if name not in config.get("services", {}):
                raise ConfigError(f"case {case_id!r} uses service {name!r}, which is not defined")


def _validate_page(page: dict, case_id: str) -> None:
    _unknown_keys(page, PAGE_KEYS, f"case {case_id!r} page")
    if not page.get("steps"):
        raise ConfigError(f"case {case_id!r}: page needs a list of steps")
    for number, step in enumerate(page["steps"], 1):
        actions = [key for key in step if key not in STEP_OPTIONS]
        where = f"case {case_id!r} step {number}"
        if len(actions) != 1:
            raise ConfigError(f"{where}: give exactly one action, one of {', '.join(sorted(STEP_ACTIONS))}")
        _unknown_keys({actions[0]: None}, STEP_ACTIONS, where)


def _unknown_keys(table: dict, known: set[str], where: str) -> None:
    for key in table:
        if key not in known:
            close = difflib.get_close_matches(key, known, n=1)
            hint = f"; did you mean {close[0]!r}?" if close else ""
            raise ConfigError(f"{where}: unknown key {key!r}{hint}")


def _find_root(start: Path) -> Path:
    for path in [start, *start.parents]:
        if (path / ".git").exists():
            return path
    return start


def _load_plugin(path: Path):
    spec = importlib.util.spec_from_file_location(f"eyes_plugin_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_config(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    for candidate in CONFIG_CANDIDATES:
        if Path(candidate).exists():
            return Path(candidate)
    raise ConfigError("no config found; run `eyes init` or pass --config PATH "
                      f"(looked for {', '.join(CONFIG_CANDIDATES)})")


def shell_quote(arg: str) -> str:
    return arg if re.fullmatch(r"[\w@%+=:,./{}-]+", arg) else "'" + arg.replace("'", "'\\''") + "'"


def rel(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def tail(text: str, limit: int = 600) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def around(text: str, needle: str, radius: int = 120) -> str:
    i = text.find(needle)
    return text[max(0, i - radius): i + len(needle) + radius]
