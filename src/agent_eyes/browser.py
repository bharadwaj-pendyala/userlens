"""Browser eyes: Playwright drives real Chromium to inspect pages and run user flows.

Without Playwright, HTML reports still get Chrome screenshots, and a
browser-engine row says why. Page flows always need Playwright.
"""

from __future__ import annotations

import contextlib
import html
import json
import re
import shutil
import subprocess
from pathlib import Path

from .core import STEP_ACTIONS, Check, Project, Result


VIEWPORTS = {"desktop": (1280, 900), "mobile": (390, 844)}


def viewport(value) -> tuple[int, int]:
    """A named viewport ("desktop", "mobile") or an explicit [width, height]."""
    return VIEWPORTS[value] if isinstance(value, str) else (int(value[0]), int(value[1]))

# Runs inside the page. Reports horizontal overflow, low-contrast text (WCAG AA)
# and elements a selector says must carry a hover title.
_INSPECT_JS = """
(titled) => {
  const parse = c => { const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
    const p = m[1].split(',').map(Number); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; };
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const bgOf = el => { for (let e = el; e; e = e.parentElement) {
    const c = parse(getComputedStyle(e).backgroundColor); if (c && c.a > 0) return c; } return {r: 255, g: 255, b: 255, a: 1}; };
  const low = [], seen = new Set(), obscured = new Set();
  // Text is obscured when something else is on top at a point inside its glyphs:
  // a fixed panel, a banner, a modal. Sample both ends and the middle of each line.
  const hidden = (node, el) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const r of [...range.getClientRects()].slice(0, 3)) {
      if (r.width < 4 || r.bottom < 0 || r.top > innerHeight) continue;
      const y = r.top + r.height / 2;
      for (const x of [r.left + 2, r.left + r.width / 2, r.right - 2]) {
        if (x < 0 || x > innerWidth) continue;
        const top = document.elementFromPoint(x, y);
        if (top && !el.contains(top) && !top.contains(el)) return true;
      }
    }
    return false;
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && seen.size < 3000) {
    const node = walker.currentNode, el = node.parentElement;
    if (!node.textContent.trim() || seen.has(el)) continue;
    seen.add(el);
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    if (hidden(node, el)) obscured.add(node.textContent.trim().slice(0, 50));
    const fg = parse(st.color), bg = bgOf(el);
    if (!fg) continue;
    const a = lum(fg), b = lum(bg), ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    const size = parseFloat(st.fontSize), large = size >= 24 || (size >= 18.66 && parseInt(st.fontWeight) >= 700);
    const need = large ? 3 : 4.5;
    if (ratio < need) low.push(`'${node.textContent.trim().slice(0, 40)}' ratio ${ratio.toFixed(2)} < ${need}`);
  }
  const doc = document.documentElement;
  const clipped = [...document.body.querySelectorAll('*')]
    .filter(e => e.getBoundingClientRect().right > doc.clientWidth + 1 && e.getClientRects().length)
    .map(e => e.tagName.toLowerCase() + (e.className ? '.' + String(e.className).split(' ')[0] : ''));
  return {
    overflow: doc.scrollWidth - doc.clientWidth,
    clipped: [...new Set(clipped)].slice(0, 8),
    low_contrast: low,
    obscured: [...obscured].slice(0, 8),
    untitled: titled ? [...document.querySelectorAll(titled)].filter(e => !e.title).length : 0,
    text_length: document.body.innerText.trim().length,
  };
}
"""


class PlaywrightEyes:
    """Drives real Chromium: blocks the network, inspects layout, saves screenshots."""

    name = "playwright"

    def __init__(self):
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()

    def _open(self, size: tuple[int, int], allow_localhost: bool, allow: tuple[str, ...] = ()):
        context = self._browser.new_context(viewport={"width": size[0], "height": size[1]})
        page = context.new_page()
        blocked, errors = [], []
        allowed = ("file:", "data:", "about:", "blob:")
        if allow_localhost:
            allowed += ("http://localhost", "http://127.0.0.1", "ws://localhost", "ws://127.0.0.1")
        allowed += tuple(allow)

        def route(request_route):
            url = request_route.request.url
            if url.startswith(allowed):
                return request_route.continue_()
            blocked.append(url)
            return request_route.abort()

        page.route("**/*", route)
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        return context, page, blocked, errors

    def look(self, page_path: Path, png: Path, size: tuple[int, int], titled: str | None) -> dict:
        context, page, blocked, errors = self._open(size, allow_localhost=False)
        page.goto(page_path.as_uri(), wait_until="load")
        facts = page.evaluate(_INSPECT_JS, titled)
        page.screenshot(path=str(png), full_page=True)
        context.close()
        return {**facts, "blocked": blocked, "errors": errors, "screenshot": str(png)}

    def flow(self, steps: list[dict], out_dir: Path, size: tuple[int, int], timeout_ms: int,
             allow: tuple[str, ...] = ()) -> dict:
        """Drive the page step by step like a user, with a screenshot after every step."""
        context, page, blocked, errors = self._open(size, allow_localhost=True, allow=allow)
        page.set_default_timeout(timeout_ms)
        log = []
        try:
            for number, step in enumerate(steps, 1):
                action = next(key for key in step if key in STEP_ACTIONS)
                png = out_dir / f"step-{number:02d}-{action}.png"
                entry = {"step": number, "action": action, "target": step[action], "screenshot": str(png)}
                try:
                    _do(page, action, step)
                    entry["status"] = "pass"
                except Exception as error:  # Playwright raises its own types; the message is what matters
                    entry["status"] = "fail"
                    entry["error"] = str(error).splitlines()[0][:300]
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(png), full_page=True)
                log.append(entry)
                if entry["status"] == "fail":
                    break
            facts = page.evaluate(_INSPECT_JS, None)
            text = page.evaluate("() => document.body ? document.body.innerText : ''")
        finally:
            context.close()
        return {**facts, "blocked": blocked, "errors": errors, "steps": log, "text": text}

    def close(self):
        self._browser.close()
        self._playwright.stop()


class ChromeEyes:
    """Fallback without Playwright: screenshots only, no in-page inspection."""

    name = "chrome"

    def __init__(self, binary: str):
        self.binary = binary

    def look(self, page_path: Path, png: Path, size: tuple[int, int], titled: str | None) -> dict:
        subprocess.run(
            [self.binary, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--host-resolver-rules=MAP * ~NOTFOUND", f"--window-size={size[0]},{size[1]}",
             f"--screenshot={png}", page_path.as_uri()],
            capture_output=True, timeout=60,
        )
        return {"screenshot": str(png)} if png.exists() else {}

    def close(self):
        pass


def _do(page, action: str, step: dict) -> None:
    target = step[action]
    if action == "goto":
        page.goto(target, wait_until="load")
    elif action == "click":
        page.locator(target).first.click(click_count=step.get("count", 1))
    elif action == "select_text":
        # How a reader selects a block: three clicks select the whole paragraph.
        page.locator(target).first.click(click_count=3)
    elif action == "fill":
        page.locator(target).first.fill(step["text"])
    elif action == "press":
        (page.locator(step["on"]).first if "on" in step else page.keyboard).press(target)
    elif action == "wait_for":
        page.locator(target).first.wait_for(state=step.get("state", "visible"))
    elif action == "expect_text":
        scope = page.locator(step.get("in", "body")).first
        scope.wait_for()
        page.wait_for_function("([el, text]) => el.innerText.includes(text)", arg=[scope.element_handle(), target])
    elif action == "expect_count":
        page.wait_for_function("([sel, n]) => document.querySelectorAll(sel).length === n",
                               arg=[target, step["count"]])
    elif action == "reload":
        page.reload(wait_until="load")
    elif action == "wait":
        page.wait_for_timeout(int(float(target) * 1000))


def open_eyes() -> tuple[object | None, str]:
    """Return (eyes, why) where ``why`` explains a downgrade, so it is never silent."""
    try:
        return PlaywrightEyes(), ""
    except Exception as error:  # missing package or browser build
        why = f"playwright unavailable ({str(error).splitlines()[0][:160]}); " \
              "fix: pip install playwright && python -m playwright install chromium"
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        if found := shutil.which(name):
            return ChromeEyes(found), why + "; using Chrome screenshots only, no layout checks"
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(mac).exists():
        return ChromeEyes(mac), why + "; using Chrome screenshots only, no layout checks"
    return None, why + "; no Chrome found either"


_ANSI_CSS = {"30": "#555", "31": "#e5534b", "32": "#57ab5a", "33": "#c69026", "34": "#539bf5",
             "35": "#b083f0", "36": "#39c5cf", "37": "#ccc", "90": "#8b949e"}


def ansi_to_html(text: str, cols: int) -> str:
    out, open_spans = [], 0
    for piece in re.split(r"(\x1b\[[0-9;]*m)", text):
        match = re.fullmatch(r"\x1b\[([0-9;]*)m", piece)
        if not match:
            out.append(html.escape(piece))
            continue
        codes = match.group(1).split(";")
        if codes in (["0"], [""]):
            out.append("</span>" * open_spans)
            open_spans = 0
            continue
        style = ";".join("font-weight:bold" if c == "1" else f"color:{_ANSI_CSS[c]}"
                         for c in codes if c == "1" or c in _ANSI_CSS)
        out.append(f'<span style="{style}">')
        open_spans += 1
    out.append("</span>" * open_spans)
    return (f"<!doctype html><meta charset=utf-8><body style='margin:0;background:#0d1117'>"
            f"<pre style='margin:0;padding:12px;color:#c9d1d9;font:14px/1.4 Menlo,monospace;"
            f"width:{cols}ch'>{''.join(out)}</pre>")


def page_checks(eyes, why: str, project: Project, result: Result, size: tuple[int, int]) -> list[Check]:
    """Run a page case's steps. Each step is one check, then layout checks on the final page."""
    if not isinstance(eyes, PlaywrightEyes):
        return [Check("page-engine", "fail", "page cases need Playwright: "
                      "pip install 'agent-eyes[browser]' && python -m playwright install chromium"
                      + (f" ({why})" if why else ""))]
    page = result.case["page"]
    facts = eyes.flow(page["steps"], result.out_dir, size, int(page.get("step_timeout", 10) * 1000),
                      tuple(page.get("allow", [])))
    result.steps = facts["steps"]
    result.page_text = facts["text"]
    source = project.sources.get("page")
    checks = []
    for entry in facts["steps"]:
        target = entry["target"] if isinstance(entry["target"], str) else json.dumps(entry["target"])
        checks.append(Check(f"step-{entry['step']:02d}-{entry['action']}", entry["status"],
                            entry.get("error", ""), target, None, source, {"artifact": entry["screenshot"]}))
    if len(facts["steps"]) < len(page["steps"]):
        checks.append(Check("steps-skipped", "fail",
                            f"{len(page['steps']) - len(facts['steps'])} step(s) did not run after a failure"))
    checks += _page_checks(facts, "page", size, source)
    shots = [entry["screenshot"] for entry in facts["steps"] if Path(entry["screenshot"]).exists()]
    if shots:
        checks.append(Check("screenshots", "pass", "open these images to see each step",
                            extra={"screenshots": shots}))
    return checks


def browser_checks(eyes, why: str, project: Project, result: Result) -> list[Check]:
    if not result.case.get("html") and not result.tty:
        return []
    if eyes is None:
        return [Check("browser-engine", "fail", f"--browser was requested but nothing can render: {why}")]
    checks = [Check("browser-engine", "skip", why)] if why else []
    shots = []
    source = project.sources.get("html")
    page = result.out_dir / result.case["html"] if result.case.get("html") else None
    if page and page.exists():
        for label, size in VIEWPORTS.items():
            facts = eyes.look(page, result.out_dir / f"html-{label}.png", size, project.config.get("titled"))
            shots += [facts["screenshot"]] if "screenshot" in facts else []
            checks += _page_checks(facts, label, size, source)
    if result.tty:
        page = result.out_dir / "terminal.html"
        page.write_text(ansi_to_html(result.stdout_raw, result.cols), encoding="utf-8")
        facts = eyes.look(page, result.out_dir / "terminal.png", (result.cols * 9 + 40, 600), None)
        shots += [facts["screenshot"]] if "screenshot" in facts else []
    expected = 2 * bool(result.case.get("html")) + bool(result.tty)
    if len(shots) < expected:
        checks.append(Check("screenshots", "fail", f"{eyes.name} saved {len(shots)} of {expected} screenshots"))
    if shots:
        checks.append(Check("screenshots", "pass", f"open these images to see the output ({eyes.name})",
                            extra={"screenshots": shots}))
    return checks


def _page_checks(facts: dict, label: str, size: tuple[int, int], source) -> list[Check]:
    if "overflow" not in facts:
        return []
    shot = {"artifact": facts["screenshot"]} if facts.get("screenshot") else {}
    return [
        Check(f"browser-{label}-network", "fail" if facts["blocked"] else "pass",
              "page tried to load: " + ", ".join(facts["blocked"][:5]) if facts["blocked"] else "", source=source),
        Check(f"browser-{label}-overflow", "fail" if facts["overflow"] > 0 or facts["clipped"] else "pass",
              f"content is wider than {size[0]}px (scrolls by {facts['overflow']}px); "
              f"elements past the right edge: {', '.join(facts['clipped']) or 'none'}"
              if facts["overflow"] > 0 or facts["clipped"] else "",
              source=source, extra=shot),
        Check(f"browser-{label}-obscured", "fail" if facts["obscured"] else "pass",
              f"{len(facts['obscured'])} piece(s) of text are covered by another element at {size[0]}px wide"
              if facts["obscured"] else "", actual="\n".join(facts["obscured"]) or None, source=source, extra=shot),
        Check(f"browser-{label}-console", "fail" if facts["errors"] else "pass",
              "; ".join(facts["errors"][:5]), source=source),
        Check(f"browser-{label}-contrast", "fail" if facts["low_contrast"] else "pass",
              f"{len(facts['low_contrast'])} text element(s) below WCAG AA contrast",
              actual="\n".join(facts["low_contrast"][:8]) or None, source=source),
        Check(f"browser-{label}-titles", "fail" if facts["untitled"] else "pass",
              f"{facts['untitled']} highlighted element(s) have no hover title" if facts["untitled"] else "",
              source=source),
        Check(f"browser-{label}-visible-text", "pass" if facts["text_length"] > 0 else "fail",
              "" if facts["text_length"] else "page shows no text", source=source, extra=shot),
    ]
