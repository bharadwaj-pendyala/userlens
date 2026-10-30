"""Run a case: prepare its workspace, start its services, run the command."""

from __future__ import annotations

import contextlib
import os
import re
import select
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .core import Project, Result


class HarnessError(Exception):
    """The case could not run as written: setup failed, a service never got ready."""


def prepare(project: Project, case: dict) -> tuple[Path, Path]:
    """Return (out_dir, cwd). A ``workspace: temp`` case runs in a fresh copy of its folder."""
    out_dir = project.artifacts / "cases" / case["id"]
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    cwd = project.base / project.config.get("cwd", ".") / case.get("cwd", ".")
    if case.get("workspace") == "temp":
        workspace = out_dir / "workspace"
        skip = project.artifacts.resolve()
        # The artifacts folder holds this very workspace, so copying it would recurse forever.
        ignore = lambda folder, names: [n for n in names if n == ".git" or (Path(folder) / n).resolve() == skip]
        try:
            shutil.copytree(cwd, workspace, symlinks=True, ignore=ignore)
        except (OSError, shutil.Error) as error:
            raise HarnessError(f"could not copy {cwd} into a temp workspace: {str(error)[:300]}") from None
        cwd = workspace
    return out_dir, cwd


def run_setup(project: Project, case: dict, cwd: Path, out_dir: Path, env: dict) -> None:
    for number, step in enumerate(case.get("setup", []), 1):
        command = project.expand(step, out_dir, cwd)
        proc = subprocess.run(command, shell=True, cwd=cwd, env=env, capture_output=True,
                              timeout=project.timeout)
        if proc.returncode != 0:
            output = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()[-400:]
            raise HarnessError(f"setup step {number} failed (exit {proc.returncode}): {command}\n{output}")


@contextlib.contextmanager
def services(project: Project, names: list[str], cwd: Path, out_dir: Path, env: dict):
    """Start each named service, wait until it is ready, and always stop it afterwards."""
    running: list[subprocess.Popen] = []
    try:
        for name in names:
            running.append(_start_service(project, name, cwd, out_dir, env))
        yield
    finally:
        for proc in running:
            _stop(proc)


def _start_service(project: Project, name: str, cwd: Path, out_dir: Path, env: dict) -> subprocess.Popen:
    spec = project.services[name]
    command = spec["command"]
    shell = isinstance(command, str)
    command = project.expand(command, out_dir, cwd) if shell else [project.expand(c, out_dir, cwd) for c in command]
    service_env = {**env, **{k: project.expand(v, out_dir, cwd) for k, v in spec.get("env", {}).items()}}
    service_cwd = Path(project.expand(spec["cwd"], out_dir, cwd)) if "cwd" in spec else cwd
    log_path = out_dir / f"service-{name}.log"
    log = log_path.open("wb")
    try:
        proc = subprocess.Popen(command, shell=shell, cwd=service_cwd, env=service_env,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as error:
        raise HarnessError(f"service {name!r} could not start: {error.strerror or error}") from None
    _wait_ready(name, spec, proc, log_path, project.expand(str(spec.get("ready", {}).get("url", "")), out_dir, cwd))
    return proc


def _wait_ready(name: str, spec: dict, proc: subprocess.Popen, log_path: Path, url: str) -> None:
    ready = spec.get("ready", {})
    deadline = time.monotonic() + spec.get("timeout", 15)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _stop(proc)
            raise HarnessError(f"service {name!r} exited with {proc.returncode} before it was ready; "
                               f"see {log_path}\n{_log_tail(log_path)}")
        if url and _responds(url):
            return
        if "log" in ready and re.search(ready["log"], log_path.read_text(errors="replace")):
            return
        if not ready:
            return
        time.sleep(0.1)
    _stop(proc)
    raise HarnessError(f"service {name!r} was not ready after {spec.get('timeout', 15)}s; "
                       f"see {log_path}\n{_log_tail(log_path)}")


def _responds(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1):
            return True
    except urllib.error.HTTPError:
        return True  # the server answered, even if with an error status
    except (urllib.error.URLError, OSError):
        return False


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _log_tail(path: Path) -> str:
    return path.read_text(errors="replace")[-400:].strip()


def base_env(project: Project, case: dict, out_dir: Path, cwd: Path) -> dict:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
        "TERM": "xterm-256color",
    }
    # Only named variables come through, so a case never depends on whoever runs it by accident.
    for name in [*project.config.get("pass_env", []), *case.get("pass_env", [])]:
        if name in os.environ:
            env[name] = os.environ[name]
    for source in (project.config.get("env", {}), case.get("env", {})):
        env.update({k: project.expand(v, out_dir, cwd) for k, v in source.items()})
    return env


def run_command(project: Project, case: dict, out_dir: Path, cwd: Path, env: dict) -> Result:
    command = [project.expand(part, out_dir, cwd) for part in case.get("command", project.config.get("command", []))]
    argv = [project.expand(arg, out_dir, cwd) for arg in case.get("argv", [])]
    stdin = _stdin(cwd, case)
    tty = case.get("tty", False)
    cols = case.get("cols", 80)
    timeout = case.get("timeout", project.timeout)

    started = time.monotonic()
    harness_error = ""
    try:
        if tty:
            code, stdout, stderr = _run_pty(command + argv, cwd, env, cols, stdin, timeout)
        else:
            proc = subprocess.run(
                command + argv, cwd=cwd, env=env, input=stdin if stdin is not None else b"",
                capture_output=True, timeout=timeout,
            )
            code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as error:
        code, stdout, stderr = -1, error.stdout or b"", error.stderr or b""
        harness_error = f"timed out after {timeout}s"
    except OSError as error:
        code, stdout, stderr = -1, b"", b""
        harness_error = f"could not start {command[0]!r}: {error.strerror or error}"

    result = Result(
        case=case, argv=argv, cwd=cwd, out_dir=out_dir, exit=code,
        stdout_raw=stdout.decode("utf-8", "replace"), stderr=stderr.decode("utf-8", "replace"),
        tty=tty, cols=cols, seconds=time.monotonic() - started, harness_error=harness_error,
    )
    (out_dir / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (out_dir / "stdout.ansi").write_text(result.stdout_raw, encoding="utf-8")
    (out_dir / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    return result


def failed_result(case: dict, out_dir: Path, cwd: Path, error: str) -> Result:
    return Result(case=case, argv=case.get("argv", []), cwd=cwd, out_dir=out_dir, exit=-1,
                  stdout_raw="", stderr="", tty=False, cols=80, seconds=0.0, harness_error=error)


def _stdin(cwd: Path, case: dict) -> bytes | None:
    if "stdin_file" in case:
        return (cwd / case["stdin_file"]).read_bytes()
    if "stdin" in case:
        return case["stdin"].encode("utf-8")
    return None


def _run_pty(cmd: list[str], cwd: Path, env: dict, cols: int, stdin: bytes | None,
             timeout: float) -> tuple[int, bytes, bytes]:
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, cols, 0, 0))
    # Given stdin, model `cat file | cmd` in a terminal: a pipe in, a terminal out.
    # Typing into the tty instead would hit the kernel's line limit (1024 bytes on macOS).
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=slave if stdin is None else subprocess.PIPE,
                            stdout=slave, stderr=subprocess.PIPE, close_fds=True)
    os.close(slave)
    stderr_chunks: list[bytes] = []
    drain = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    drain.start()
    if stdin is not None:
        threading.Thread(target=_feed, args=(proc.stdin, stdin), daemon=True).start()

    chunks = []
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() >= deadline:
            proc.kill()
            os.close(master)
            raise subprocess.TimeoutExpired(cmd, timeout, b"".join(chunks), b"")
        ready, _, _ = select.select([master], [], [], 0.2)
        if not ready:
            if proc.poll() is not None:
                break
            continue
        try:
            data = os.read(master, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    os.close(master)
    code = proc.wait(timeout=max(deadline - time.monotonic(), 1))
    drain.join(timeout=5)
    return code, b"".join(chunks).replace(b"\r\n", b"\n"), b"".join(stderr_chunks)


def _feed(pipe, data: bytes) -> None:
    try:
        pipe.write(data)
        pipe.close()
    except OSError:
        pass  # the command exited without reading all of its input
