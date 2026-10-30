"""The `eyes` command: run, approve, list, init."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from . import __version__
from .core import ConfigError, Project, find_config
from .report import approve, run

EXAMPLES = """\
Examples:
  eyes init                      write eyes/eyes.json with starter cases
  eyes run                       run every case and write artifacts/eyes/report.json
  eyes run --case help           run one case (repeatable)
  eyes run --browser             also screenshot HTML and terminal output
  eyes approve --case help       accept the output you inspected as the snapshot

Exit codes:
  0  every check passed
  1  at least one check failed (read artifacts/eyes/report.json)
  2  the config or the command line is wrong
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eyes", description="Let an agent see your CLI or web UI the way a user does.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="path to eyes.json (default: search the usual places)")
    parser.add_argument("--version", action="version", version=f"eyes {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run cases, check them, and write the report")
    run_p.add_argument("--case", action="append", help="run only this case (repeatable)")
    run_p.add_argument("--browser", action="store_true", help="screenshot HTML and terminal output")
    approve_p = sub.add_parser("approve", help="accept the inspected output as the reviewed snapshot")
    group = approve_p.add_mutually_exclusive_group(required=True)
    group.add_argument("--case", action="append")
    group.add_argument("--all", action="store_true")
    sub.add_parser("list", help="list case ids")
    init_p = sub.add_parser("init", help="write a starter eyes/eyes.json for this project")
    init_p.add_argument("--command", dest="cli", help="how to run your CLI, e.g. 'node bin/tool.js'")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            return init(Path.cwd(), args.cli)
        project = Project(find_config(args.config))
        if args.command == "list":
            for case in project.cases():
                print(case["id"])
            return 0
        if args.command == "approve":
            return approve(project, None if args.all else args.case)
        return run(project, args.case, args.browser)
    except ConfigError as error:
        print(f"eyes: {error}", file=sys.stderr)
        return 2


def init(root: Path, cli: str | None) -> int:
    target = root / "eyes" / "eyes.json"
    if target.exists():
        print(f"eyes: {target.relative_to(root)} already exists; edit it instead", file=sys.stderr)
        return 2
    command = cli.split() if cli else _guess_command(root)
    config = {
        "version": 1,
        "command": command or ["REPLACE-ME"],
        "cwd": ".",
        "cases": [
            {"id": "help", "argv": ["--help"], "exit": 0},
            {"id": "no-args", "argv": [], "exit": 0},
            {"id": "unknown-flag", "argv": ["--no-such-flag"], "exit": 2},
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    _ignore_artifacts(root)
    print(f"wrote {target.relative_to(root)}")
    if not command:
        print("  set \"command\" to how you run your CLI, e.g. [\"node\", \"bin/tool.js\"]")
    print("next:\n  eyes run                 see what a user sees\n"
          "  eyes approve --all       accept the output once it looks right\n"
          "  add cases to eyes/eyes.json; see the README for every key")
    return 0


def _guess_command(root: Path) -> list[str]:
    package = root / "package.json"
    if package.exists():
        bins = json.loads(package.read_text(encoding="utf-8")).get("bin")
        path = next(iter(bins.values())) if isinstance(bins, dict) and bins else bins
        if isinstance(path, str):
            return ["node", str((root / path).relative_to(root))]
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        scripts = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("scripts", {})
        if scripts:
            module = next(iter(scripts.values())).split(":")[0].rsplit(".", 1)[0]
            return ["{python}", "-m", module]
    return []


def _ignore_artifacts(root: Path) -> None:
    gitignore = root / ".gitignore"
    if gitignore.exists() and "artifacts/" not in gitignore.read_text(encoding="utf-8"):
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write("\nartifacts/eyes/\n")
