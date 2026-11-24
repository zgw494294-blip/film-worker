"""Stage a trusted Git release and supervise the local worker process."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import bundles
from .protocol import digest
from .settings import ROOT, atomic_json
from .state import instance_lock

BASELINE = {"commit": "", "version": "local", "sequence": 0}
RESTART_FOR_UPDATE = 75
RELEASE_PROTOCOL = 1
REF = "refs/film-worker/candidate"


def platform_key():
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    system = "macos" if platform.system() == "Darwin" else platform.system().lower()
    return f"{system}-{arch}"


def executable(project):
    relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return project / ".venv" / relative


def run(args, *, cwd=None, env=None, timeout=120):
    try:
        return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=timeout).stdout.strip()
    except subprocess.CalledProcessError as exc:
        # Git output can contain remote credentials; expose only the command category.
        raise ValueError(f"{Path(args[0]).name} 执行失败（退出码 {exc.returncode}）") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{Path(args[0]).name} 执行超时") from exc


class Releases:
    def __init__(self, root):
        self.root = root
        self.data = root / "updates"
        self.data.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data / "state.json"
        self.repository = self.data / "git"

    def state(self):
        if self.state_file.exists():
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        return {"active": BASELINE, "previous": None, "pending": None, "floor": 0, "error": ""}

    def write(self, data):
        atomic_json(self.state_file, data)

    def directory(self, release):
        commit = release["commit"]
        if not commit:
            return ROOT
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValueError("程序提交号格式无效")
        return self.data / "releases" / commit

    def stage(self, config, *, tools=None):
        if not config["update_remote"]:
            raise ValueError("请填写 Git 发布仓库地址")
        remote = config["update_remote"]
        parsed = urlsplit(remote)
        local = Path(remote).is_absolute() and Path(remote).exists()
        trusted_transport = (parsed.scheme in {"https", "ssh"} and parsed.hostname and not parsed.password
                             and not parsed.fragment and not parsed.query)
        if not local and not trusted_transport:
            raise ValueError("更新地址须为 HTTPS、SSH 或本机 Git 仓库的绝对路径")
        tag = config["update_tag"]
        signer_path = Path(config["update_signers"]).expanduser()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", tag) or not signer_path.is_file():
            raise ValueError("请填写发布标签和本机可信发布公钥文件")
        git, uv = tools or (shutil.which("git"), shutil.which("uv"))
        if not git:
            raise ValueError("找不到 Git，自动更新需要 Git")
        uv = os.environ.get("FILM_WORKER_UV") or uv or bundled_uv(ROOT)
        if not uv:
            raise ValueError("找不到 uv，无法准备新版 Python 依赖")
        git_env = {k: v for k, v in os.environ.items() if k not in {"FILM_WORKER_TOKEN", "GIT_DIR", "GIT_WORK_TREE"}}
        git_env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
        with instance_lock(self.data / "stage-lock"):
            if not (self.repository / "HEAD").exists():
                run([git, "init", "--bare", str(self.repository)], env=git_env)
            options = [git, "-C", str(self.repository)]
            run([*options, "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                 "-c", "protocol.ssh.allow=always", "-c", f"protocol.file.allow={'always' if local else 'never'}",
                 "fetch", "--no-tags", "--", remote, f"+refs/tags/{tag}:{REF}"], env=git_env)
            run([*options, "-c", "gpg.format=ssh", "-c", "gpg.minTrustLevel=fully",
                 "-c", f"gpg.ssh.allowedSignersFile={signer_path.resolve()}", "verify-tag", REF], env=git_env)
            commit = run([*options, "rev-parse", "--verify", REF + "^{commit}"], env=git_env)
            meta = json.loads(run([*options, "show", f"{commit}:release.json"], env=git_env))
            seq = meta.get("sequence")
            state = self.state()
            if not isinstance(meta.get("version"), str) or type(seq) is not int or seq < 1:
                raise ValueError("发布清单缺少版本或序号")
            if seq <= state["floor"]:
                return None
            if (platform_key() not in meta.get("platforms", []) or
                    meta.get("protocol") != RELEASE_PROTOCOL or meta.get("state_format") != 1):
                raise ValueError("更新包与本机系统、工作协议或数据格式不兼容")
            candidate = {"commit": commit, "version": meta["version"], "sequence": seq}
            project = self.directory(candidate)
            if not (project / ".verified.json").exists():
                self.prepare(options, uv, commit, meta, project, git_env)
            return candidate

    def prepare(self, git, uv, commit, meta, project, env):
        project.parent.mkdir(parents=True, exist_ok=True)
        archive_path = project.with_suffix(".zip.partial")
        if project.exists():
            shutil.rmtree(project)
        try:
            run([*git, "archive", "--format=zip", "--output", str(archive_path), commit], env=env)
            # The virtual environment must be created in its final path on both systems.
            bundles.extract(archive_path, project, indexed=False)
            if meta["lock_sha256"] != digest(project / "uv.lock"):
                raise ValueError("发布清单的依赖摘要与实际锁文件不一致")
            runtime_env = {**env, "UV_PROJECT_ENVIRONMENT": str(project / ".venv")}
            base_python = getattr(sys, "_base_executable", sys.executable)
            run([uv, "sync", "--locked", "--no-install-project", "--python", base_python],
                cwd=project, env=runtime_env, timeout=240)
            with tempfile.TemporaryDirectory(prefix="worker-update-check-") as tmp:
                run([str(executable(project)), "-m", "film_worker", "--data", tmp, "self-test"],
                    cwd=project, env=runtime_env, timeout=45)
            atomic_json(project / ".verified.json", {"commit": commit})
        except BaseException:
            shutil.rmtree(project, ignore_errors=True)
            raise
        finally:
            archive_path.unlink(missing_ok=True)

    def queue(self, candidate, rollback=False):
        state = self.state()
        state.update(pending=candidate, error="")
        if not rollback:
            state["floor"] = max(state["floor"], candidate["sequence"])
        self.write(state)


def bundled_uv(project):
    name = "uv.exe" if os.name == "nt" else "uv"
    for parent in (project, Path(os.environ.get("FILM_WORKER_BOOTSTRAP", ROOT))):
        path = parent / "runtime" / name
        if path.is_file():
            return str(path)
    return None


def launcher(root, port, browser, test_http):
    releases = Releases(root)
    # A single launcher owns the active pointer, but the child owns the separate worker lock.
    with instance_lock(releases.data / "launcher-lock"):
        while True:
            state = releases.state()
            selected = state["pending"] or state["active"]
            pending = state["pending"] is not None
            try:
                project = releases.directory(selected)
                python = executable(project) if selected["commit"] else Path(sys.executable)
                port, code = launch_panel(project, python, root, port, browser, test_http, releases, selected, state)
            except (ValueError, OSError) as exc:
                if not pending:
                    raise
                state.update(pending=None, error=f"新版启动失败，已恢复原版 · {exc}")
                releases.write(state)
                continue
            if code != RESTART_FOR_UPDATE:
                return code
            browser = False


def launch_panel(project, python, root, port, browser, test_http, releases, selected, state):
    run_id = uuid.uuid4().hex
    handshake = releases.data / "launch" / run_id
    handshake.mkdir(parents=True)
    env = dict(os.environ, FILM_WORKER_LAUNCH=str(handshake), FILM_WORKER_RELEASE=selected["commit"],
               FILM_WORKER_BOOTSTRAP=str(ROOT))
    startup = [str(python), "-m", "film_worker", "--data", str(root)]
    if test_http:
        startup.append("--test-http")
    command = [*startup, "panel", "--port", str(port), "--no-browser"]
    child = None
    try:
        child = subprocess.Popen(command, cwd=project, env=env)
        ready = handshake / "ready.json"
        limit = time.monotonic() + 45
        while not ready.is_file():
            if child.poll() is not None:
                raise ValueError(f"程序在启动时退出（退出码 {child.returncode}）")
            if time.monotonic() > limit:
                raise ValueError("控制界面启动超时")
            time.sleep(0.1)
        report = json.loads(ready.read_text(encoding="utf-8"))
        if state["pending"] is not None:
            releases.write({**state, "previous": state["active"], "active": selected, "pending": None})
        atomic_json(handshake / "activate.json", {"active": True})
        if browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{report['port']}")
        return report["port"], child.wait()
    except KeyboardInterrupt:
        return port, 0
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=7)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        shutil.rmtree(handshake, ignore_errors=True)
