# agent-eyes

Let a coding agent see your CLI or web UI the way a user does, then tell it
what to fix.

`eyes` runs your product in real situations: in a pipe, in a terminal of a set
width, or as a page in Chromium driven step by step. It checks what a user
would notice and compares the output with snapshots that you reviewed. Then it
writes a report that names each problem, shows the evidence, and points at the
file that most likely needs the edit.

The core is Python stdlib only. Page flows and screenshots use Playwright.

## Install

```bash
pip install agent-eyes                          # terminal and HTML checks
pip install 'agent-eyes[browser]'               # adds page flows and screenshots
python -m playwright install chromium
```

`eyes` tests any CLI or web UI, in any language. Your project does not need to
use Python.

## Start

```bash
eyes init           # writes eyes/eyes.json with three starter cases
eyes run            # runs every case, writes artifacts/eyes/report.json
eyes approve --all  # accepts the output once you have read it
```

`eyes init` guesses your CLI from `package.json` (`bin`) or `pyproject.toml`
(`[project.scripts]`, which must be installed in the same environment as
`eyes`). Pass `--command 'node bin/tool.js'` to set it yourself.

## Commands

```bash
eyes run                       # all cases
eyes run --case help           # one case (repeatable)
eyes run --browser             # also screenshot HTML files and colored terminal output
eyes approve --case help       # accept the output you inspected
eyes list
```

`eyes run` exits 1 when a check fails. A config error exits 2. `eyes` looks for
the config in `eyes.json`, `eyes/eyes.json`, `tests/ux/eyes.json` or
`tests/eyes/eyes.json`. Use `--config PATH` to pick another.

## What it writes

| File | Use |
|---|---|
| `artifacts/eyes/report.json` | Read this first. `failures` lists every failed check. |
| `artifacts/eyes/report.txt` | The same report for people. |
| `artifacts/eyes/cases/<id>/stdout.txt` | Output without color codes. `stdout.ansi` keeps them. |
| `artifacts/eyes/cases/<id>/snapshot.diff` | How the output changed from the reviewed snapshot. |
| `artifacts/eyes/cases/<id>/*.png` | Screenshots. Open them to see the page or the terminal. |
| `artifacts/eyes/cases/<id>/service-*.log` | Output of each background service. |

Each failure in `report.json` has `check`, `detail`, `expected`, `actual`,
`source` (the file to edit, from your config) and `reproduce.shell`.

## What it checks

Every command case:

- The exit code, and that no Python traceback reaches the user.
- The expected text on stdout and stderr, as plain text or a regex.
- No color codes in piped output or under `NO_COLOR`, and color in a real TTY.
- Line width in terminal cells, at the case width (80 when piped).
- Count and noun agree, so the output never says "1 files".
- Strict JSON when the case says the output is JSON.
- HTML files are one document that loads nothing from the network.
- The output matches the reviewed snapshot.

Every page case, and HTML files under `--browser`:

- Each step runs, with a screenshot after it. A failed step stops the flow.
- The page loads nothing outside localhost.
- No horizontal overflow, no text hidden under another element (a fixed panel,
  banner or modal), no console errors, and text contrast meets WCAG AA.

## The config

```json
{
  "version": 1,
  "command": ["node", "bin/tool.js"],
  "cwd": "eyes/fixtures",
  "sources": {"cli": "bin/tool.js"},
  "cases": [
    {"id": "help", "argv": ["--help"], "stdout_contains": ["Usage", "Examples"]},
    {"id": "typo", "argv": ["--hepl"], "exit": 2, "stderr_contains": ["did you mean"]}
  ]
}
```

Top-level keys:

| Key | Meaning |
|---|---|
| `version` | Always `1`. |
| `command` | How to run your CLI. `{python}` is the Python that runs eyes. |
| `cwd` | Folder the cases run in, relative to the config. |
| `env` | Extra environment variables for every case. |
| `pass_env` | Names of variables to copy from your shell, such as `DATABASE_URL`. Cases start from a clean environment, so nothing else leaks in. |
| `timeout` | Seconds before a case is stopped (default 30). |
| `sources` | Files to name in failures: `cli`, `terminal`, `json`, `html`, `page`. |
| `plugins` | Python files with `checks(result, project) -> list[Check]`. |
| `services` | Background processes that cases can start. See below. |
| `plural_nouns`, `titled`, `artifacts`, `snapshots` | Tuning; see the source. |

Case keys:

| Key | Meaning |
|---|---|
| `argv`, `command` | Arguments, and an optional per-case command. `{out}` is the case artifact folder. |
| `exit` | Expected exit code (default 0). |
| `tty`, `cols` | Run in a real terminal of this width. With `stdin`, stdin is a pipe. |
| `stdin`, `stdin_file`, `env`, `pass_env`, `cwd`, `timeout` | Input and environment. |
| `workspace: "temp"` | Run in a fresh copy of `cwd`, so the case can change files safely. `{workspace}` is its path. The copy leaves out `.git` and the artifacts folder; run `git init` in `setup` if the case needs a repo. |
| `setup` | Shell commands to run in the workspace first, for example `git init`. |
| `services` | Names of services to start before the case and stop after it. |
| `stdout_contains`, `stdout_absent`, `stdout_regex`, `stdout_empty` | Checks on stdout. The same `stderr_` keys exist. |
| `expect_ansi`, `check_width` | Color must appear; set `check_width: false` for machine formats. |
| `json`, `html`, `html_contains` | Output is JSON; the case writes this HTML file; its visible text has these strings. |
| `page` | Makes this a page case. See below. |

`eyes` rejects unknown keys and suggests the closest one, so a typo never
turns a check off.

## Services and page flows

A service is a background process that a case needs, such as a dev server.
`eyes` starts it, waits until `ready` succeeds, runs the case, then stops it.
Its output goes to `service-<name>.log`.

```json
"services": {
  "site": {
    "command": ["python3", "-m", "http.server", "4612"],
    "cwd": "{workspace}/demo",
    "ready": {"url": "http://localhost:4612/"}
  }
}
```

`ready` takes a `url` that must answer, or a `log` regex that must appear.

A page case drives the UI in Chromium like a user does:

```json
{"id": "comment-flow", "workspace": "temp", "services": ["site"],
 "page": {"viewport": "desktop", "steps": [
   {"goto": "http://localhost:4612/index.html"},
   {"select_text": "article p"},
   {"click": "#add-comment"},
   {"fill": "#comment textarea", "text": "too vague"},
   {"click": "[data-save]"},
   {"expect_count": ".card", "count": 1},
   {"reload": true},
   {"expect_text": "too vague", "in": ".rail"}
 ]}}
```

Steps: `goto`, `click` (with `count`), `select_text`, `fill` (with `text`),
`press` (with `on`), `wait_for` (with `state`), `expect_text` (with `in`),
`expect_count` (with `count`), `reload`, `wait` (seconds). `viewport` is
`desktop`, `mobile` or `[width, height]`. `step_timeout` is in seconds.

A page may load from localhost only. List other origins the flow needs in
`allow`, for example `"allow": ["https://fonts.gstatic.com/", "https://api.example.com/"]`.
Any other request is blocked and fails the network check.

## The agent loop

1. Run `eyes run --browser`.
2. Read `artifacts/eyes/report.json`. Start with `failures`.
3. Open the screenshots and `stdout.txt` files of the failing cases. Read them
   as a first-time user would.
4. Fix the file in `source`. Change a check only when the check is wrong.
5. Run again until only `snapshot` checks fail.
6. Read each `snapshot.diff`. Approve the changes that improve the output.
7. Commit.

Write a case before you change the product, and watch it fail first.

### Prompt for a UX improvement loop

```text
Improve the UX of this project with `eyes` as your eyes.
1. Run `eyes run --browser`. Read artifacts/eyes/report.json.
2. Open the screenshots and stdout.txt files. List what a first-time user and
   a CI user would find confusing, noisy, or missing. Try the product on real
   inputs too, not only the fixtures.
3. Rank the problems by how many users hit them and how badly. Skip rare
   edge cases.
4. For each problem worth fixing, add a case to eyes/eyes.json that fails today.
5. Fix the product, not the check. Keep each fix small.
6. Rerun until only snapshot checks fail. Review every snapshot.diff, then
   approve only the changes that improve the output.
7. Ask a second agent to review the diff from a user's point of view.
8. Commit with a message that says why.
Stop when the remaining problems are rare edge cases.
```

## License

MIT
