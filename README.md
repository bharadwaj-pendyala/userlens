# userlens

[![CI](https://github.com/bharadwaj-pendyala/userlens/actions/workflows/ci.yml/badge.svg)](https://github.com/bharadwaj-pendyala/userlens/actions/workflows/ci.yml)

Check that what you built actually works, the way a user would use it. Then
check that it is good to use.

A coding agent can write a feature and pass its unit tests, and the product can
still be broken: the command crashes on real input, the button does nothing,
the file never gets written. `eyes` closes that gap. It runs your CLI or web UI
end to end, like a user does, and reports what happened with evidence an agent
can read: the output, the files left behind, and a screenshot of every step.

Use it to answer three questions after every change:

1. **Does it work?** Run the real command or click through the real page, then
   check the exit code, the output, the JSON values and the files it wrote.
2. **Did anything else break?** Compare every case with the output you reviewed
   last time.
3. **Is it good to use?** Catch the problems users notice: stack traces, color
   codes in logs, text wider than the terminal, broken plurals, unreadable
   contrast, and text hidden under a panel.

`eyes` works with a CLI or web UI in any language. The core is Python stdlib
only. Page flows and screenshots use Playwright.

## Install

```bash
pip install 'userlens[browser]'
python -m playwright install chromium
```

Leave out `[browser]` and the Playwright step if you only test a CLI. The
package is `userlens`; its command is `eyes` (`userlens` works too). It runs on
macOS and Linux. Windows is not supported yet.

## Start

```bash
eyes init           # writes eyes/eyes.json and teaches your coding agent the loop
eyes run            # runs every case, writes artifacts/eyes/report.json
eyes approve --all  # accepts the output once you have read it
```

`eyes init` also adds a short "Verify changes with eyes" section to
`AGENTS.md` (read by Codex and most agents) and `CLAUDE.md` (read by Claude
Code). It creates either file if needed. The section tells the agent to write
cases before code, run them, read the report and screenshots, and approve only
intended changes. So any coding agent in the repo follows the loop without a
prompt. The section sits between `<!-- eyes:start -->` and `<!-- eyes:end -->`;
running `eyes init` again refreshes it and leaves the rest of the file alone.
Add `--no-agent-docs` to skip it. On a repo that already has a config, `eyes
init` keeps the config and only writes this section.

`eyes init` guesses your CLI from `package.json` (`bin`) or `pyproject.toml`
(`[project.scripts]`, which must be installed in the same environment as
`eyes`). Pass `--command 'node bin/tool.js'` to set it yourself.

## Verify a feature works

Write a case for what the feature must do, before or right after you build it.
Each case is an executable statement of the expected behavior.

A CLI feature: `tool init --agent codex` must succeed, say so, and write a
config with that agent.

```json
{"id": "init-writes-config", "workspace": "temp",
 "argv": ["init", "--agent", "codex"],
 "exit": 0,
 "stdout_contains": ["configured (agent: codex)"],
 "files": {".tool/config.json": {"json": {"agent": "codex"}}}}
```

A machine-readable output: `tool check hard.md --json` must report a failure
for that file.

```json
{"id": "json-verdict", "argv": ["check", "hard.md", "--json"], "exit": 1,
 "stdout_json": {"files.0.verdict": "fail", "summary.failed": 1}}
```

A web feature: leaving a comment must add it to the sidebar, keep it after a
reload, and save it to disk.

```json
{"id": "comment-flow", "workspace": "temp", "services": ["api", "site"],
 "page": {"steps": [
   {"goto": "http://localhost:4612/post.html"},
   {"select_text": "article p"},
   {"click": "#add-comment"},
   {"fill": "#comment textarea", "text": "too vague"},
   {"click": "[data-save]"},
   {"expect_count": ".card", "count": 1},
   {"reload": true},
   {"expect_text": "too vague", "in": ".sidebar"}
 ]},
 "files": {".tool/comments.json": {"contains": ["too vague"]}}}
```

Error paths are features too. A user who mistypes must get a clear message and
the right exit code:

```json
{"id": "typo", "argv": ["deev"], "exit": 2, "stderr_contains": ["did you mean 'dev'"]}
```

Run `eyes run --case <id>` while you build. Run `eyes run` before you commit.

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
| `artifacts/eyes/cases/<id>/step-NN-*.png` | A screenshot after each page step. Open them to see what the user saw. |
| `artifacts/eyes/cases/<id>/workspace/` | The temp copy the case ran in, with every file it wrote. |
| `artifacts/eyes/cases/<id>/service-*.log` | Output of each background service. |
| `artifacts/eyes/cases/<id>/snapshot.diff` | How the output changed from the reviewed snapshot. |

Each failure in `report.json` has `check`, `detail`, `expected`, `actual`,
`source` (the file to edit, from your config) and `reproduce.shell`.

## What it checks

**It works.** Set these per case:

- The exit code.
- Text on stdout and stderr, as plain text or a regex.
- Values in JSON output, by dotted path such as `files.0.verdict`.
- Files the case wrote: that they exist or not, their text, and JSON values.
- Each page step: the click lands, the element appears, the text shows up.
  A failed step stops the flow and names the step.

**Nothing else broke.** Every case is compared with its reviewed snapshot: the
full output for a command, and the step list plus the visible page text for a
page.

**It is good to use.** These run on every case without any setup:

- No stack trace reaches the user (Python, Node, Rust, Go).
- No color codes in piped output or under `NO_COLOR`, and color in a real TTY.
- Lines fit the terminal width, measured in terminal cells.
- Count and noun agree, so the output never says "1 files".
- JSON output is strict JSON. HTML output is one document that loads nothing
  from the network.
- On pages: nothing loads from outside localhost unless allowed, no horizontal
  overflow, no text hidden under another element, no console errors, and text
  contrast meets WCAG AA.

## The config

```json
{
  "version": 1,
  "command": ["node", "{root}/bin/tool.js"],
  "cwd": "..",
  "sources": {"cli": "bin/tool.js"},
  "cases": [
    {"id": "help", "argv": ["--help"], "stdout_contains": ["usage:"]}
  ]
}
```

Top-level keys:

| Key | Meaning |
|---|---|
| `version` | Always `1`. |
| `command` | How to run your CLI. `{python}` is the Python that runs eyes, `{root}` is the repo root. |
| `cwd` | Folder the cases run in, relative to the config. |
| `env` | Extra environment variables for every case. |
| `pass_env` | Names of variables to copy from your shell, such as `DATABASE_URL`. Cases start from a clean environment, so nothing else leaks in. |
| `timeout` | Seconds before a case is stopped (default 30). |
| `sources` | Files to name in failures: `cli`, `terminal`, `json`, `html`, `page`. |
| `plugins` | Python files with `checks(result, project) -> list[Check]`, for checks no key covers. |
| `services` | Background processes that cases can start. See below. |
| `plural_nouns`, `titled`, `artifacts`, `snapshots` | Tuning; see the source. |

Case keys:

| Key | Meaning |
|---|---|
| `argv`, `command` | Arguments, and an optional per-case command. `{out}` is the case artifact folder. |
| `exit` | Expected exit code (default 0). |
| `stdout_contains`, `stdout_absent`, `stdout_regex`, `stdout_empty` | Checks on stdout. The same `stderr_` keys exist. |
| `stdout_json` | `{"dotted.path": value}` pairs that the JSON on stdout must hold. |
| `files` | `{"path": {...}}` for files the case must leave behind. Each takes `exists` (default `true`), `contains`, `absent` and `json` (dotted paths). Paths are relative to where the case ran. |
| `workspace: "temp"` | Run in a fresh copy of `cwd`, so the case can change files safely. `{workspace}` is its path. The copy leaves out `.git` and the artifacts folder; run `git init` in `setup` if the case needs a repo. |
| `setup` | Shell commands to run in the workspace first. |
| `services` | Names of services to start before the case and stop after it. |
| `page` | Makes this a page case. See below. |
| `tty`, `cols` | Run in a real terminal of this width. With `stdin`, stdin is a pipe. |
| `stdin`, `stdin_file`, `env`, `pass_env`, `cwd`, `timeout` | Input and environment. |
| `expect_ansi`, `check_width` | Color must appear; set `check_width: false` for machine formats. |
| `json`, `html`, `html_contains` | Output is JSON; the case writes this HTML file; its visible text has these strings. |
| `note` | Free text for people. |

`eyes` rejects unknown keys and page steps and suggests the closest one, so a
typo never turns a check off.

## Services and page flows

A service is a background process that a case needs, such as an API or a dev
server. `eyes` starts it, waits until `ready` succeeds, runs the case, then
stops it. Its output goes to `service-<name>.log`.

```json
"services": {
  "site": {
    "command": ["python3", "-m", "http.server", "4612"],
    "cwd": "{workspace}/public",
    "ready": {"url": "http://localhost:4612/"}
  }
}
```

`ready` takes a `url` that must answer, or a `log` regex that must appear.

Page steps: `goto`, `click` (with `count`), `select_text`, `fill` (with
`text`), `press` (with `on`), `wait_for` (with `state`), `expect_text` (with
`in`), `expect_count` (with `count`), `reload`, `wait` (seconds). Page keys:
`viewport` (`desktop`, `mobile` or `[width, height]`), `step_timeout` in
seconds, and `allow`.

A page may load from localhost only. List other origins the flow needs in
`allow`, for example `"allow": ["https://fonts.gstatic.com/", "https://api.example.com/"]`.
Any other request is blocked and fails the network check.

## The agent loop

When an agent builds or changes a feature:

1. Write cases for what the feature must do, including its error paths.
2. Run `eyes run --case <id>` and watch the new cases fail.
3. Build until they pass. Read `artifacts/eyes/report.json` after each run,
   starting with `failures`.
4. Open the screenshots, `stdout.txt` and the workspace files of the cases you
   touched. Confirm the product does the right thing, not just that the checks
   pass.
5. Run `eyes run --browser` for everything. Fix what you broke.
6. Read each `snapshot.diff`. Approve only the changes you meant to make.
7. Commit.

Fix the product, not the check. Change a check only when the check is wrong.

### Prompt: build and verify a feature

```text
Build <feature>. Use `eyes` to prove it works the way a user would use it.
1. Before you write code, add cases to eyes/eyes.json for what the feature
   must do: the happy path, the outputs and files it must produce, and the
   errors a user can hit. Run them and confirm they fail.
2. Build the feature until the cases pass.
3. Open the screenshots and outputs of those cases. Check that the result is
   right, not only that the checks pass.
4. Run `eyes run` for every case. Fix anything you broke.
5. Review each snapshot.diff and approve only intended changes. Then commit.
```

### Prompt: improve the user experience

```text
Improve the UX of this project with `eyes` as your eyes.
1. Run `eyes run --browser`. Read artifacts/eyes/report.json.
2. Open the screenshots and stdout.txt files. Try the product on real inputs
   too. List what a first-time user and a CI user would find confusing,
   noisy, or broken.
3. Rank the problems by how many users hit them and how badly. Skip rare
   edge cases.
4. For each problem worth fixing, add a case that fails today, then fix the
   product until it passes.
5. Review every snapshot.diff, approve only improvements, and commit.
Stop when the remaining problems are rare edge cases.
```

## License

MIT
