"""Checks that apply to any command: exit code, streams, color, width, JSON, HTML."""

from __future__ import annotations

import json
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

from .core import ANSI, Check, Project, Result, around, tail


def generic_checks(project: Project, result: Result) -> list[Check]:
    case = result.case
    src = project.sources
    if result.harness_error:
        return [Check("runs", "fail", result.harness_error, source=src.get("cli"),
                      actual=tail(result.stderr) or None)]
    if result.is_page:
        return []
    checks = [_check_exit(result, src.get("cli"))]
    checks.append(_check_no_traceback(result, src.get("cli")))
    checks.extend(_check_text_expectations(result, src.get("cli")))
    checks.append(_check_ansi(result, src.get("terminal")))
    if _is_terminal_text(result) and case.get("check_width", True):
        checks.append(_check_width(result, src.get("terminal")))
        checks.append(_check_plurals(result, project.nouns, src.get("terminal")))
    if case.get("json"):
        checks.append(_check_json_parses(result, src.get("json")))
    if case.get("html"):
        checks.extend(_check_html(result.out_dir / case["html"], src.get("html")))
        checks.extend(_check_html_contains(result.out_dir / case["html"], case.get("html_contains", []),
                                           src.get("html")))
    return checks


_MISSING = object()


def outcome_checks(project: Project, result: Result) -> list[Check]:
    """Did it work? The values it printed and the files it left behind, for commands and page flows alike."""
    source = project.sources.get("cli")
    checks = []
    if "stdout_json" in result.case:
        checks += _check_stdout_json(result, source)
    for path, spec in result.case.get("files", {}).items():
        checks.append(_check_file(project.expand(path, result.out_dir, result.cwd), spec, result, source))
    return checks


def value_at(data, path: str):
    """Follow a dotted path such as ``files.0.score.verdict`` into parsed JSON."""
    for part in path.split("."):
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            return _MISSING
    return data


def _check_stdout_json(result: Result, source) -> list[Check]:
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return [Check("stdout-json", "fail", "stdout is not JSON, so its values cannot be checked",
                      actual=tail(result.stdout), source=source)]
    checks = []
    for path, want in result.case["stdout_json"].items():
        got = value_at(data, path)
        expected = f"{path} = {json.dumps(want)}"
        if got is _MISSING:
            checks.append(Check("stdout-json", "fail", f"no value at {path!r}", expected, None, source))
        else:
            ok = got == want
            checks.append(Check("stdout-json", "pass" if ok else "fail", "" if ok else f"{path} has the wrong value",
                                expected, None if ok else json.dumps(got), source))
    return checks


def _check_file(path: str, spec: dict, result: Result, source) -> Check:
    name = f"file {path}"
    target = result.cwd / path
    if not spec.get("exists", True):
        ok = not target.exists()
        return Check(name, "pass" if ok else "fail", "" if ok else "this file should not exist", source=source)
    if not target.is_file():
        return Check(name, "fail", "the file was not written", f"a file at {path}", source=source,
                     extra={"artifact": str(result.cwd)})
    text = target.read_text(encoding="utf-8", errors="replace")
    problems = [f"missing {needle!r}" for needle in spec.get("contains", []) if needle not in text]
    problems += [f"should not contain {needle!r}" for needle in spec.get("absent", []) if needle in text]
    if "json" in spec:
        try:
            data = json.loads(text)
        except ValueError:
            problems.append("not valid JSON")
        else:
            for key, want in spec["json"].items():
                got = value_at(data, key)
                if got is _MISSING:
                    problems.append(f"no value at {key!r}")
                elif got != want:
                    problems.append(f"{key} is {json.dumps(got)}, expected {json.dumps(want)}")
    return Check(name, "fail" if problems else "pass", "; ".join(problems),
                 actual=tail(text) if problems else None, source=source, extra={"artifact": str(target)})


def _check_exit(result: Result, source) -> Check:
    want = result.case.get("exit", 0)
    ok = result.exit == want
    return Check("exit-code", "pass" if ok else "fail", "" if ok else "unexpected exit code",
                 str(want), str(result.exit), source)


# Python, Node (two or more "at" frames), Rust and Go stack traces.
_STACK_TRACE = re.compile(
    r"Traceback \(most recent call last\)"
    r"|(?:^[ \t]+at [^\n]+:\d+:\d+\)?[ \t]*\n){2}"
    r"|^thread '[^']*' panicked at"
    r"|^goroutine \d+ \[running\]:",
    re.MULTILINE,
)


def _check_no_traceback(result: Result, source) -> Check:
    leaked = _STACK_TRACE.search(result.stderr + result.stdout)
    return Check("no-traceback", "fail" if leaked else "pass",
                 "a stack trace reached the user; catch the error and print one line that says what to do" if leaked else "",
                 source=source, actual=tail(result.stderr) if leaked else None)


def _check_text_expectations(result: Result, source) -> list[Check]:
    case = result.case
    checks = []
    for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        for needle in case.get(f"{stream}_contains", []):
            ok = needle in text
            checks.append(Check(f"{stream}-contains", "pass" if ok else "fail",
                                "" if ok else f"{stream} is missing expected text",
                                needle, None if ok else tail(text), source))
        for needle in case.get(f"{stream}_absent", []):
            ok = needle not in text
            checks.append(Check(f"{stream}-absent", "pass" if ok else "fail",
                                "" if ok else f"{stream} contains text that must not appear",
                                f"no {needle!r}", None if ok else around(text, needle), source))
        for pattern in case.get(f"{stream}_regex", []):
            ok = re.search(pattern, text) is not None
            checks.append(Check(f"{stream}-regex", "pass" if ok else "fail",
                                "" if ok else f"no {stream} line matches the pattern",
                                pattern, None if ok else tail(text), source))
    if case.get("stdout_empty"):
        ok = result.stdout.strip() == ""
        checks.append(Check("stdout-empty", "pass" if ok else "fail",
                            "" if ok else "stdout must be empty", "", None if ok else tail(result.stdout), source))
    return checks


def _check_ansi(result: Result, source) -> Check:
    has_ansi = bool(ANSI.search(result.stdout_raw + result.stderr))
    want = result.case.get("expect_ansi", False)
    ok = has_ansi == want
    detail = ""
    if not ok:
        detail = "expected color codes" if want else "color codes leaked into non-color output (pipe or NO_COLOR)"
    return Check("ansi", "pass" if ok else "fail", detail, source=source)


def _is_terminal_text(result: Result) -> bool:
    case = result.case
    return not case.get("json") and not result.stdout.lstrip().startswith(("{", "[", "<"))


def _check_width(result: Result, source) -> Check:
    limit = result.cols
    too_wide = []
    for number, line in enumerate((result.stdout + result.stderr).splitlines(), 1):
        if cells(line) <= limit:
            continue
        if any(len(token) > limit // 2 for token in line.split()):
            continue  # an unbreakable token, such as a long path, is allowed to overflow
        too_wide.append((number, cells(line), line))
    if not too_wide:
        return Check("line-width", "pass", source=source)
    number, width, line = too_wide[0]
    return Check("line-width", "fail",
                 f"{len(too_wide)} line(s) wider than {limit} columns; first is line {number}",
                 f"<= {limit} columns", f"{width} columns: {line}", source,
                 {"artifact_line": number})


def cells(text: str) -> int:
    """Terminal columns used by ``text``: wide CJK and emoji take two, combining marks none."""
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in text)


def _check_plurals(result: Result, nouns: list[str], source) -> Check:
    text = result.stdout + result.stderr
    words = "|".join(map(re.escape, nouns))
    one_plural = re.search(rf"(?<![\d.])\b1 ({words})s\b", text)
    many_singular = re.search(rf"(?<![\d.])\b(0|[2-9]|[1-9]\d+) ({words})\b(?!s)", text)
    bad = one_plural or many_singular
    if not bad:
        return Check("pluralization", "pass", source=source)
    return Check("pluralization", "fail", "count and noun disagree", None, bad.group(), source,
                 {"artifact_line": text.count("\n", 0, bad.start()) + 1})


def _check_json_parses(result: Result, source) -> Check:
    try:
        json.loads(result.stdout, parse_constant=_reject_constant)
    except ValueError as error:
        return Check("json-parses", "fail", f"stdout is not strict JSON: {error}",
                     source=source, actual=tail(result.stdout))
    return Check("json-parses", "pass", source=source)


def _reject_constant(name: str):
    raise ValueError(f"non-standard JSON constant {name}")


class _HTMLScan(HTMLParser):
    LOADERS = {"script", "link", "img", "iframe", "object", "embed", "video", "audio", "source"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []
        self.documents = 0
        self.text: list[str] = []
        self._skip = 0

    def handle_decl(self, decl):
        if decl.lower().startswith("doctype"):
            self.documents += 1

    def handle_starttag(self, tag, attrs):
        if tag in self.LOADERS:
            self.problems.append(f"<{tag}> element loads or runs external content")
        attributes = dict(attrs)
        if tag == "meta" and (attributes.get("http-equiv") or "").lower() == "refresh":
            self.problems.append("<meta http-equiv=refresh> navigates away")
        for name, value in attrs:
            value = value or ""
            if name.startswith("on"):
                self.problems.append(f"<{tag} {name}> runs script")
            if name in ("src", "srcset") or (name == "href" and not value.startswith("#")):
                self.problems.append(f"<{tag} {name}={value!r}> references a resource")
            if name == "style" and ("url(" in value or "@import" in value):
                self.problems.append(f"<{tag} style> loads a resource")
        if tag in ("style", "head"):
            self._skip += 1
        if tag == "br":
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "head"):
            self._skip -= 1
        if tag in ("div", "p", "li", "h1", "h2", "section", "aside", "main", "tr"):
            self.text.append("\n")

    def handle_data(self, data):
        if self._skip:
            if "@import" in data or "url(" in data:
                self.problems.append("inline CSS loads a resource")
            return
        self.text.append(data)


def html_text(path: Path) -> str:
    scan = _HTMLScan()
    scan.feed(path.read_text(encoding="utf-8"))
    lines = [line.strip() for line in "".join(scan.text).splitlines()]
    return "\n".join(line for line in lines if line)


def _check_html(path: Path, source) -> list[Check]:
    if not path.exists():
        return [Check("html-written", "fail", f"{path.name} was not written", source=source)]
    scan = _HTMLScan()
    scan.feed(path.read_text(encoding="utf-8"))
    return [
        Check("html-written", "pass", source=source),
        Check("html-single-document", "pass" if scan.documents == 1 else "fail",
              "" if scan.documents == 1 else "output must be exactly one HTML document",
              "1", str(scan.documents), source),
        Check("html-offline", "fail" if scan.problems else "pass",
              "; ".join(sorted(set(scan.problems))), source=source),
    ]


def _check_html_contains(path: Path, needles: list[str], source) -> list[Check]:
    if not needles or not path.exists():
        return []
    text = html_text(path)
    return [Check("html-contains", "pass" if n in text else "fail",
                  "" if n in text else "visible page text is missing expected text", n, None, source)
            for n in needles]
