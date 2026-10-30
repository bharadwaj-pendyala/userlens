import json
import subprocess
import sys

import pytest

from agent_eyes import ConfigError, Project
from agent_eyes import browser, checks, report, runner
from agent_eyes.cli import init, main


def _project(tmp_path, cases, **config):
    (tmp_path / ".git").mkdir(exist_ok=True)
    path = tmp_path / "eyes.json"
    path.write_text(json.dumps({"version": 1, "command": ["sh", "-c"], "cases": cases, **config}))
    return Project(path)


def _result(project, case_id):
    case = next(c for c in project.cases() if c["id"] == case_id)
    out_dir, cwd = runner.prepare(project, case)
    env = runner.base_env(project, case, out_dir, cwd)
    return runner.run_command(project, case, out_dir, cwd, env)


def _checks(project, case_id):
    return {c.check: c for c in checks.generic_checks(project, _result(project, case_id))}


def _full(project, case_id):
    case = next(c for c in project.cases() if c["id"] == case_id)
    _, case_checks = report._run_one(project, case, None, "", False)
    return {c.check: c for c in case_checks}


def _report(project):
    report.run(project, None, browser_wanted=False)
    return json.loads((project.artifacts / "report.json").read_text())


# config

def test_config_without_a_version_is_rejected(tmp_path):
    (tmp_path / "eyes.json").write_text(json.dumps({"command": ["true"], "cases": []}))
    with pytest.raises(ConfigError, match="version"):
        Project(tmp_path / "eyes.json")


def test_misspelled_case_key_gets_a_suggestion(tmp_path):
    with pytest.raises(ConfigError, match="did you mean 'stdout_contains'"):
        _project(tmp_path, [{"id": "a", "stdout_contain": ["x"]}])


@pytest.mark.parametrize("bad_id", ["/tmp/x", "../up", "", "a b"])
def test_case_ids_that_could_escape_the_artifacts_folder_are_rejected(tmp_path, bad_id):
    with pytest.raises(ConfigError):
        _project(tmp_path, [{"id": bad_id}])


def test_duplicate_case_ids_are_rejected(tmp_path):
    with pytest.raises(ConfigError):
        _project(tmp_path, [{"id": "a"}, {"id": "a"}])


def test_undefined_service_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not defined"):
        _project(tmp_path, [{"id": "a", "services": ["web"]}])


def test_bad_config_exits_2_with_one_line(tmp_path, monkeypatch, capsys):
    (tmp_path / "eyes.json").write_text("{not json")
    monkeypatch.chdir(tmp_path)
    assert main(["run"]) == 2
    assert capsys.readouterr().err.startswith("eyes: ")


# running commands

def test_slow_command_becomes_a_failed_check_not_a_crash(tmp_path):
    project = _project(tmp_path, [{"id": "slow", "argv": ["sleep 5"]}], timeout=0.5)
    assert _checks(project, "slow")["runs"].status == "fail"


def test_slow_tty_command_is_killed_at_the_timeout(tmp_path):
    project = _project(tmp_path, [{"id": "slow", "argv": ["sleep 5"], "tty": True}], timeout=0.5)
    assert "timed out" in _checks(project, "slow")["runs"].detail


def test_missing_executable_becomes_a_failed_check(tmp_path):
    project = _project(tmp_path, [{"id": "x", "command": ["no-such-binary-xyz"]}])
    assert "could not start" in _checks(project, "x")["runs"].detail


def test_tty_case_receives_stdin(tmp_path):
    project = _project(tmp_path, [{"id": "echo", "argv": ["read line; echo got:$line"], "tty": True,
                                   "stdin": "hello\n", "stdout_contains": ["got:hello"]}])
    assert _checks(project, "echo")["stdout-contains"].status == "pass"


def test_long_stdin_in_a_tty_case_does_not_time_out(tmp_path):
    project = _project(tmp_path, [{"id": "cat", "argv": ["cat | wc -c"], "tty": True,
                                   "stdin": "x" * 5000 + "\n"}], timeout=5)
    assert "runs" not in _checks(project, "cat")


def test_temp_workspace_keeps_the_source_folder_clean(tmp_path):
    (tmp_path / "site").mkdir()
    project = _project(tmp_path, [{"id": "w", "cwd": "site", "workspace": "temp",
                                   "setup": ["echo made > made.txt"], "argv": ["cat made.txt"],
                                   "stdout_contains": ["made"]}])
    assert _full(project, "w")["stdout-contains"].status == "pass"
    assert not (tmp_path / "site" / "made.txt").exists()


def test_failed_setup_is_reported_with_its_output(tmp_path):
    project = _project(tmp_path, [{"id": "s", "setup": ["echo broken >&2; exit 3"], "argv": ["true"]}])
    row = next(r for r in _report(project)["failures"] if r["check"] == "runs")
    assert "setup step 1 failed" in row["detail"] and "broken" in row["detail"]


def test_service_is_ready_before_the_case_and_stopped_after(tmp_path):
    service = {"command": [sys.executable, "-m", "http.server", "47321", "--bind", "127.0.0.1"],
               "ready": {"url": "http://127.0.0.1:47321/"}}
    fetch = f"{sys.executable} -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:47321/').status)\""
    project = _project(tmp_path, [{"id": "web", "services": ["web"], "argv": [fetch],
                                   "stdout_contains": ["200"]}], services={"web": service})
    assert _full(project, "web")["stdout-contains"].status == "pass"
    assert subprocess.run(["sh", "-c", fetch], capture_output=True).returncode != 0


def test_service_that_dies_fails_the_case_with_its_log(tmp_path):
    service = {"command": "echo cannot bind >&2; exit 1", "ready": {"url": "http://127.0.0.1:47322/"}}
    project = _project(tmp_path, [{"id": "c", "services": ["svc"], "argv": ["true"]}], services={"svc": service})
    row = next(r for r in _report(project)["failures"] if r["check"] == "runs")
    assert "exited" in row["detail"] and "cannot bind" in row["detail"]


# snapshots and report

def test_approve_accepts_the_inspected_run_without_rerunning(tmp_path):
    project = _project(tmp_path, [{"id": "a", "argv": ["echo one"]}])
    report.run(project, None, browser_wanted=False)
    project.config["cases"][0]["argv"] = ["echo two"]
    assert report.approve(project, None) == 0
    assert "one" in (project.snapshots / "a.txt").read_text()


def test_approve_without_a_captured_run_fails(tmp_path):
    assert report.approve(_project(tmp_path, [{"id": "a", "argv": ["echo one"]}]), None) == 1


def test_first_run_of_a_new_case_shows_the_captured_output(tmp_path):
    row = next(r for r in _report(_project(tmp_path, [{"id": "new", "argv": ["echo hi"]}]))["failures"])
    assert row["check"] == "snapshot" and "hi" in row["actual"]


def test_reproduce_keeps_multiline_stdin_intact(tmp_path):
    project = _project(tmp_path, [{"id": "a", "argv": ["cat"], "stdin": "one\ntwo\n"}])
    shell = report._reproduce(project, _result(project, "a"))["shell"]
    assert subprocess.run(["sh", "-c", shell], capture_output=True, text=True).stdout == "one\ntwo\n"


# checks

def test_html_scan_flags_meta_refresh_and_inline_handlers(tmp_path):
    page = tmp_path / "p.html"
    page.write_text('<!doctype html><meta http-equiv="refresh" content="0;url=https://x">'
                    '<body onload="fetch(1)">hi</body>')
    offline = {c.check: c for c in checks._check_html(page, None)}["html-offline"]
    assert offline.status == "fail"
    assert "refresh" in offline.detail and "onload" in offline.detail


def test_wide_characters_count_as_two_terminal_cells():
    assert checks.cells("日本") == 4
    assert checks.cells("abc") == 3


def test_browser_requested_but_unavailable_is_a_failure(tmp_path):
    project = _project(tmp_path, [{"id": "a", "argv": ["echo hi"], "tty": True}])
    result = _result(project, "a")
    assert any(c.status == "fail" for c in browser.browser_checks(None, "no browser", project, result))


# page flows

playwright = pytest.importorskip("playwright.sync_api")


def _page_project(tmp_path, steps):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text(
        "<!doctype html><title>t</title><p id=intro>Hello reader.</p>"
        "<button id=go onclick=\"document.querySelector('#intro').textContent='Clicked.'\">Go</button>")
    service = {"command": [sys.executable, "-m", "http.server", "47323", "--bind", "127.0.0.1"],
               "cwd": "{workspace}", "ready": {"url": "http://127.0.0.1:47323/"}}
    return _project(tmp_path, [{"id": "flow", "cwd": "site", "services": ["web"],
                                "page": {"steps": steps, "step_timeout": 3}}], services={"web": service})


def test_page_flow_runs_each_step_and_screenshots_it(tmp_path):
    project = _page_project(tmp_path, [{"goto": "http://127.0.0.1:47323/index.html"},
                                       {"click": "#go"}, {"expect_text": "Clicked.", "in": "#intro"}])
    rows = _report(project)["checks"]
    steps = [r for r in rows if r["check"].startswith("step-")]
    assert [r["status"] for r in steps] == ["pass", "pass", "pass"]
    assert all((project.artifacts / "cases" / "flow" / f"step-0{i}-{a}.png").exists()
               for i, a in ((1, "goto"), (2, "click"), (3, "expect_text")))


def test_failed_step_stops_the_flow_and_names_the_step(tmp_path):
    project = _page_project(tmp_path, [{"goto": "http://127.0.0.1:47323/index.html"},
                                       {"click": "#missing"}, {"click": "#go"}])
    failures = {r["check"]: r for r in _report(project)["failures"]}
    assert "step-02-click" in failures and "steps-skipped" in failures


# init

def test_init_guesses_a_node_cli_and_writes_starter_cases(tmp_path, capsys):
    (tmp_path / "package.json").write_text(json.dumps({"bin": {"tool": "bin/tool.js"}}))
    assert init(tmp_path, None) == 0
    config = json.loads((tmp_path / "eyes" / "eyes.json").read_text())
    assert config["command"] == ["node", "bin/tool.js"] and config["version"] == 1
    assert init(tmp_path, None) == 2


def test_init_config_runs_from_the_project_root(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tool.sh").write_text("echo hello from tool\n")
    init(tmp_path, "sh bin/tool.sh")
    project = Project(tmp_path / "eyes" / "eyes.json")
    assert "hello from tool" in _result(project, "help").stdout


@pytest.mark.parametrize("trace", [
    "Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: x\n",
    "Error: boom\n    at main (/app/bin/tool.js:3:9)\n    at Object.<anonymous> (/app/bin/tool.js:9:1)\n",
    "thread 'main' panicked at src/main.rs:2:5:\nboom\n",
    "panic: boom\n\ngoroutine 1 [running]:\nmain.main()\n",
])
def test_stack_traces_from_common_runtimes_are_caught(tmp_path, trace):
    script = tmp_path / "trace.txt"
    script.write_text(trace)
    project = _project(tmp_path, [{"id": "t", "argv": [f"cat {script} >&2; exit 1"], "exit": 1}])
    assert _checks(project, "t")["no-traceback"].status == "fail"


def test_temp_workspace_of_the_project_root_skips_artifacts_and_git(tmp_path):
    (tmp_path / "src.txt").write_text("source")
    project = _project(tmp_path, [{"id": "w", "workspace": "temp", "argv": ["ls -a"],
                                   "stdout_contains": ["src.txt"], "stdout_absent": ["artifacts", ".git"]}],
                       artifacts="{base}/artifacts/eyes")
    found = _full(project, "w")
    assert found["stdout-contains"].status == "pass"
    assert found["stdout-absent"].status == "pass"


def test_an_unexpected_harness_error_fails_one_case_and_the_run_continues(tmp_path, monkeypatch):
    project = _project(tmp_path, [{"id": "bad", "argv": ["true"]}, {"id": "good", "argv": ["echo ok"]}])
    real = runner.run_command

    def explode(project_, case, *args):
        if case["id"] == "bad":
            raise RuntimeError("boom")
        return real(project_, case, *args)

    monkeypatch.setattr(runner, "run_command", explode)
    rows = _report(project)["checks"]
    assert any(r["case"] == "bad" and r["check"] == "runs" and "boom" in r["detail"] for r in rows)
    assert any(r["case"] == "good" and r["check"] == "exit-code" and r["status"] == "pass" for r in rows)


def test_text_hidden_under_a_fixed_panel_is_flagged(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text(
        "<!doctype html><title>t</title><h1 id=title>A long heading that runs under the panel</h1>"
        "<aside style='position:fixed;top:0;right:0;width:70%;height:100%;background:#eee'>panel</aside>")
    service = {"command": [sys.executable, "-m", "http.server", "47324", "--bind", "127.0.0.1"],
               "cwd": "{workspace}", "ready": {"url": "http://127.0.0.1:47324/"}}
    project = _project(tmp_path, [{"id": "cover", "cwd": "site", "services": ["web"],
                                   "page": {"steps": [{"goto": "http://127.0.0.1:47324/index.html"}]}}],
                       services={"web": service})
    failures = {r["check"]: r for r in _report(project)["failures"]}
    assert "browser-page-obscured" in failures
    assert "A long heading" in failures["browser-page-obscured"]["actual"]


def test_misspelled_page_step_is_rejected_with_a_suggestion(tmp_path):
    with pytest.raises(ConfigError, match="did you mean 'expect_text'"):
        _project(tmp_path, [{"id": "p", "page": {"steps": [{"expect_txt": "hi"}]}}])


def test_init_uses_the_declared_console_script(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "tiny"\n[project.scripts]\ntiny = "tiny.cli:main"\n')
    init(tmp_path, None)
    assert json.loads((tmp_path / "eyes" / "eyes.json").read_text())["command"] == ["tiny"]


def test_pass_env_lets_named_variables_through(tmp_path, monkeypatch):
    monkeypatch.setenv("EYES_TEST_TOKEN", "secret-value")
    monkeypatch.setenv("EYES_OTHER", "hidden")
    project = _project(tmp_path, [{"id": "e", "argv": ["echo $EYES_TEST_TOKEN-$EYES_OTHER"],
                                   "stdout_contains": ["secret-value-"], "stdout_absent": ["hidden"]}],
                       pass_env=["EYES_TEST_TOKEN"])
    found = _checks(project, "e")
    assert found["stdout-contains"].status == "pass" and found["stdout-absent"].status == "pass"


def test_reproduce_for_a_service_case_is_a_runnable_eyes_command(tmp_path):
    project = _project(tmp_path, [{"id": "s", "workspace": "temp", "argv": ["true"]}])
    shell = report._reproduce(project, _result(project, "s"))["shell"]
    assert shell.startswith("eyes --config ") and shell.endswith(" run --case s")


def test_page_allow_list_lets_named_hosts_through(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text(
        "<!doctype html><title>t</title><p>hi</p><img src='https://cdn.example.invalid/x.png'>")
    service = {"command": [sys.executable, "-m", "http.server", "47325", "--bind", "127.0.0.1"],
               "cwd": "{workspace}", "ready": {"url": "http://127.0.0.1:47325/"}}
    steps = [{"goto": "http://127.0.0.1:47325/index.html"}]
    blocked = _project(tmp_path, [{"id": "b", "cwd": "site", "services": ["web"], "page": {"steps": steps}}],
                       services={"web": service})
    assert "browser-page-network" in {r["check"] for r in _report(blocked)["failures"]}
    allowed = _project(tmp_path, [{"id": "a", "cwd": "site", "services": ["web"],
                                   "page": {"steps": steps, "allow": ["https://cdn.example.invalid/"]}}],
                       services={"web": service})
    assert "browser-page-network" not in {r["check"] for r in _report(allowed)["failures"]}
