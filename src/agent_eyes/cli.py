"""The `eyes` command: run, approve, list, init."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from . import __version__
from .core import CONFIG_CANDIDATES, ConfigError, Project, find_config
from .report import approve, run

EXAMPLES = """\
Examples:
  eyes init                      write eyes/eyes.json and an eyes section in AGENTS.md and CLAUDE.md
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
        prog="eyes", description="Check that your CLI or web UI works the way a user uses it, then that it is good to use.",
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
    init_p = sub.add_parser("init", help="write a starter config and teach coding agents to use eyes")
    init_p.add_argument("--command", dest="cli", help="how to run your CLI, e.g. 'node bin/tool.js'")
    init_p.add_argument("--no-agent-docs", action="store_true",
                        help="do not add the eyes section to AGENTS.md and CLAUDE.md")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            return init(Path.cwd(), args.cli, agent_docs=not args.no_agent_docs)
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


def init(root: Path, cli: str | None, agent_docs: bool = True) -> int:
    existing = next((root / c for c in CONFIG_CANDIDATES if (root / c).is_file()), None)
    target = existing or root / "eyes" / "eyes.json"
    command = cli.split() if cli else _guess_command(root)
    if existing:
        print(f"kept {target.relative_to(root)}")
    else:
        config = {
            "version": 1,
            "command": command or ["REPLACE-ME"],
            "cwd": "..",
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
    if agent_docs:
        for name in AGENT_DOCS:
            print(f"{_write_agent_block(root / name, target.relative_to(root).as_posix())} {name}")
    print("next:\n  eyes run                 see what a user sees\n"
          "  eyes approve --all       accept the output once it looks right\n"
          f"  add cases to {target.relative_to(root).as_posix()}; see the README for every key")
    return 0


# Codex and most agents read AGENTS.md; Claude Code reads CLAUDE.md.
AGENT_DOCS = ("AGENTS.md", "CLAUDE.md")
BLOCK_START, BLOCK_END = "<!-- eyes:start -->", "<!-- eyes:end -->"
AGENT_BLOCK = """{start}
## Verify changes with eyes

This repo uses [agent-eyes](https://github.com/bharadwaj-pendyala/agent-eyes)
to check that the product works the way a user uses it. The cases are in
`{config}`.

When you build or change a feature:

1. Before you write code, add cases for what the feature must do: the happy
   path, the output and files it must produce, and the errors a user can hit.
   Run `eyes run --case <id>` and confirm that the new cases fail.
2. Build until they pass. After each run, read `artifacts/eyes/report.json`,
   starting with `failures`.
3. Open the screenshots and `stdout.txt` files of the cases you touched.
   Confirm the result is right, not only that the checks pass.
4. Run `eyes run` for every case before you finish. Fix what you broke.
5. Read each `artifacts/eyes/cases/<id>/snapshot.diff`. Run
   `eyes approve --case <id>` only for changes you meant to make.

Fix the product, not the check. Change a check only when the check is wrong.
{end}
"""


def _write_agent_block(path: Path, config: str) -> str:
    block = AGENT_BLOCK.format(start=BLOCK_START, end=BLOCK_END, config=config)
    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return "wrote"
    text = path.read_text(encoding="utf-8")
    if BLOCK_START in text and BLOCK_END in text:
        before, rest = text.split(BLOCK_START, 1)
        after = rest.split(BLOCK_END, 1)[1].lstrip("\n")
        path.write_text(before + block + after, encoding="utf-8")
        return "updated"
    path.write_text(text.rstrip("\n") + "\n\n" + block, encoding="utf-8")
    return "added a section to"


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
            return [next(iter(scripts))]  # the console script, as users type it
    return []


def _ignore_artifacts(root: Path) -> None:
    gitignore = root / ".gitignore"
    if gitignore.exists() and "artifacts/" not in gitignore.read_text(encoding="utf-8"):
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write("\nartifacts/eyes/\n")
