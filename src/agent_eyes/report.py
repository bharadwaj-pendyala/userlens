"""Run every case, check it, diff its snapshot, and write the report agents read."""

from __future__ import annotations

import difflib
import json
import shutil
import sys

from . import browser, runner
from .checks import generic_checks, html_text, outcome_checks
from .core import Check, Project, Result, rel, shell_quote


def run(project: Project, only: list[str] | None, browser_wanted: bool) -> int:
    cases = project.cases(only)
    needs_browser = browser_wanted or any("page" in case for case in cases)
    eyes, why = browser.open_eyes() if needs_browser else (None, "")
    rows = []
    try:
        for case in cases:
            result, checks = _run_one(project, case, eyes, why, browser_wanted)
            for check in checks:
                row = {"case": case["id"], **{k: v for k, v in vars(check).items() if v not in (None, "", {})}}
                if check.status == "fail":
                    row["reproduce"] = _reproduce(project, result)
                    row["artifacts"] = rel(result.out_dir)
                rows.append(row)
    finally:
        if eyes:
            eyes.close()
    return _write_report(project, rows)


def _run_one(project: Project, case: dict, eyes, why: str, browser_wanted: bool) -> tuple[Result, list[Check]]:
    out_dir = project.artifacts / "cases" / case["id"]
    cwd = project.base
    checks: list[Check] = []
    try:
        out_dir, cwd = runner.prepare(project, case)
        env = runner.base_env(project, case, out_dir, cwd)
        runner.run_setup(project, case, cwd, out_dir, env)
        with runner.services(project, case.get("services", []), cwd, out_dir, env):
            if "page" in case:
                result = runner.failed_result(case, out_dir, cwd, "")
                size = browser.viewport(case["page"].get("viewport", "desktop"))
                checks += _guarded(lambda: browser.page_checks(eyes, why, project, result, size))
            else:
                result = runner.run_command(project, case, out_dir, cwd, env)
    except runner.HarnessError as error:
        result = runner.failed_result(case, out_dir, cwd, str(error))
    except Exception as error:  # a bug in eyes or a plugin must cost one case, not the whole report
        result = runner.failed_result(case, out_dir, cwd, f"eyes hit an internal error: {error!r}")

    checks = generic_checks(project, result) + checks
    if not result.harness_error:
        checks += outcome_checks(project, result)
        for plugin in project.plugins:
            checks.extend(plugin.checks(result, project))
        checks.append(_check_snapshot(project, result, snapshot_text(project, result)))
        if browser_wanted and "page" not in case:
            checks += _guarded(lambda: browser.browser_checks(eyes, why, project, result))
    return result, checks


def _guarded(make_checks) -> list[Check]:
    try:
        return make_checks()
    except Exception as error:  # one broken page must not lose the whole report
        return [Check("browser-engine", "fail", f"browser crashed: {str(error)[:300]}")]


def snapshot_text(project: Project, result: Result) -> str:
    if result.is_page:
        parts = [f"page {result.case['page']['steps'][0].get('goto', '')}".rstrip()]
        parts += [f"  {s['step']:>2}. {s['action']}"
                  + ("" if isinstance(s["target"], bool) else f" {s['target']}") + f"  [{s['status']}]"
                  for s in result.steps]
        parts += ["--- visible text", result.page_text.strip()]
    else:
        shown = [*result.case.get("command", []), *result.argv]  # a per-case command is part of what ran
        parts = [f"$ {' '.join(shell_quote(project.expand(a, result.out_dir, result.cwd)) for a in shown)}",
                 f"[exit {result.exit}{' tty ' + str(result.cols) + ' cols' if result.tty else ''}]",
                 "--- stdout", result.stdout.rstrip("\n"), "--- stderr", result.stderr.rstrip("\n")]
        if result.case.get("html"):
            path = result.out_dir / result.case["html"]
            if path.exists():
                parts += [f"--- {result.case['html']} (visible text)", html_text(path)]
    text = "\n".join(parts) + "\n"
    return text.replace(str(result.out_dir), "{out}").replace(str(project.root), "{root}")


def _check_snapshot(project: Project, result: Result, actual: str) -> Check:
    path = project.snapshots / f"{result.case['id']}.txt"
    (result.out_dir / "snapshot.txt").write_text(actual, encoding="utf-8")
    if not path.exists():
        return Check("snapshot", "fail", "new case: no reviewed snapshot yet. Read the output below; "
                     "if it is right, run `eyes approve --case " + result.case["id"] + "`",
                     "a reviewed snapshot at " + rel(path), actual[:1500], None,
                     {"artifact": str(result.out_dir / "snapshot.txt")})
    expected = path.read_text(encoding="utf-8")
    if expected == actual:
        return Check("snapshot", "pass")
    diff = "".join(difflib.unified_diff(expected.splitlines(True), actual.splitlines(True),
                                        "snapshot", "actual"))
    (result.out_dir / "snapshot.diff").write_text(diff, encoding="utf-8")
    return Check("snapshot", "fail", "output changed; review the diff, then approve or fix",
                 actual=diff[:1500], extra={"artifact": str(result.out_dir / "snapshot.diff")})


def _reproduce(project: Project, result: Result) -> dict:
    case = result.case
    if "page" in case or case.get("workspace") or case.get("setup") or case.get("services"):
        return {"shell": f"eyes --config {shell_quote(rel(project.config_path))} run --case {case['id']}"}
    command = [project.expand(part, result.out_dir, result.cwd)
               for part in case.get("command", project.config.get("command", []))] + result.argv
    env = {k: project.expand(v, result.out_dir, result.cwd)
           for source in (project.config.get("env", {}), case.get("env", {})) for k, v in source.items()}
    line = " ".join([*(f"{k}={shell_quote(v)}" for k, v in env.items()), *(shell_quote(a) for a in command)])
    if "stdin_file" in case:
        line += f" < {shell_quote(case['stdin_file'])}"
    elif "stdin" in case:
        line = f"printf '%s' {shell_quote(case['stdin'])} | {line}"
    elif not result.tty:
        line += " < /dev/null"
    return {
        "shell": f"cd {shell_quote(str(result.cwd))} && {line}",
        "note": f"run this in a real terminal {result.cols} columns wide" if result.tty else None,
        "timeout": case.get("timeout", project.timeout),
    }


def _write_report(project: Project, rows: list[dict]) -> int:
    evaluated = [r for r in rows if r["status"] != "skip"]
    failures = [r for r in rows if r["status"] == "fail"]
    score = round(100 * (len(evaluated) - len(failures)) / max(len(evaluated), 1), 1)
    summary = {
        "score": score,
        "checks": len(evaluated),
        "failed": len(failures),
        "skipped": len(rows) - len(evaluated),
        "cases": len({r["case"] for r in rows}),
        "failing_cases": sorted({r["case"] for r in failures}),
        "screenshots": [s for r in rows for s in r.get("extra", {}).get("screenshots", [])],
    }
    report = {"summary": summary, "failures": failures, "checks": rows}
    project.artifacts.mkdir(parents=True, exist_ok=True)
    (project.artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    lines = [f"eyes: {summary['cases']} cases, {summary['checks']} checks, "
             f"{summary['failed']} failed, {summary['skipped']} skipped, score {score}"]
    for row in failures:
        lines.append(f"\nFAIL {row['case']} :: {row['check']}"
                     + (f"  ({row['source']})" if row.get("source") else ""))
        if row.get("detail"):
            lines.append(f"  {row['detail']}")
        if row.get("expected") is not None:
            lines.append(f"  expected: {row['expected']}")
        if row.get("actual"):
            lines.append("  actual:   " + row["actual"].replace("\n", "\n            "))
        if row.get("extra", {}).get("artifact"):
            lines.append(f"  see:      {row['extra']['artifact']}")
    if summary["screenshots"]:
        lines.append("\nscreenshots:")
        lines += [f"  {s}" for s in summary["screenshots"]]
    lines.append(f"\nfull report: {project.artifacts / 'report.json'}")
    text = "\n".join(lines) + "\n"
    (project.artifacts / "report.txt").write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 1 if failures else 0


def approve(project: Project, only: list[str] | None) -> int:
    """Accept the output that `run` captured and you inspected; never rerun."""
    project.snapshots.mkdir(parents=True, exist_ok=True)
    missing = []
    for case in project.cases(only):
        captured = project.artifacts / "cases" / case["id"] / "snapshot.txt"
        if not captured.exists():
            missing.append(case["id"])
            continue
        target = project.snapshots / f"{case['id']}.txt"
        shutil.copyfile(captured, target)
        print(f"approved {rel(target)}")
    if missing:
        print(f"eyes: no captured run for {', '.join(missing)}; run them first", file=sys.stderr)
        return 1
    return 0
