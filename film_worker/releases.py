"""Stage a trusted Git release and supervise the local worker process."""
from __future__ import annotations

import json
from contextlib import ExitStack
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
from .settings import ROOT, atomic_json, bundled_release
from .startup import log_directory, running_panel
from .state import InstanceBusy, instance_lock

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
                              errors="replace", timeout=timeout,
                              creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)).stdout.strip()
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
        bundled = bundled_release()
        floor = bundled[1]["sequence"] if bundled else 0
        return {"active": BASELINE, "previous": None, "pending": None, "floor": floor, "error": ""}

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
            # The signed lock digest applies to repository bytes on every host.
            run([*git, "-c", "core.autocrlf=false", "archive", "--format=zip", "--output", str(archive_path), commit], env=env)
            # The virtual environment must be created in its final path on both systems.
            bundles.extract(archive_path, project, indexed=False)
            if meta["lock_sha256"] != digest(project / "uv.lock"):
                raise ValueError("发布清单的依赖摘要与实际锁文件不一致")
            runtime_env = {**env, "UV_PROJECT_ENVIRONMENT": str(project / ".venv")}
            base_python = getattr(sys, "_base_executable", sys.executable)
            requirements = portable_requirements(meta["lock_sha256"])
            if requirements:
                offline_env = {**runtime_env, "UV_OFFLINE": "1", "UV_NO_CONFIG": "1", "UV_PYTHON_DOWNLOADS": "never"}
                run([uv, "lock", "--check", "--offline", "--python", base_python], cwd=project, env=offline_env)
                run([uv, "venv", "--offline", "--no-project", "--python", base_python, str(project / ".venv")],
                    cwd=project, env=offline_env)
                run([uv, "pip", "sync", "--require-hashes", "--no-index", "--offline",
                     "--find-links", str(requirements.with_name("wheels")), "--python", str(executable(project)),
                     str(requirements)], cwd=project, env=offline_env, timeout=240)
            else:
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


def portable_requirements(lock_sha256):
    bootstrap = Path(os.environ.get("FILM_WORKER_BOOTSTRAP", ROOT))
    lock = bootstrap / "uv.lock"
    runtime = bootstrap / "runtime"
    requirements = runtime / "wheels.txt"
    manifest = runtime / "manifest.json"
    python = Path(os.environ.get("FILM_WORKER_BOOTSTRAP_PYTHON", sys.executable))
    marker = python.parent.parent.parent / "installed.txt"
    if not (lock.is_file() and digest(lock) == lock_sha256 and marker.is_file()
            and manifest.is_file() and requirements.is_file() and (runtime / "wheels").is_dir()):
        return None
    if marker.read_text(encoding="ascii") != digest(manifest):
        raise ValueError("安装包依赖清单与已验证的本机安装不一致")
    contents = json.loads(manifest.read_text(encoding="utf-8"))
    expected = next((item["sha256"] for item in contents["files"] if item["path"] == "wheels.txt"), None)
    if expected != digest(requirements):
        raise ValueError("安装包依赖清单的摘要不符")
    return requirements


def reopen(root, browser):
    url = running_panel(root)
    if url and browser:
        import webbrowser
        webbrowser.open(url)
        print(url, flush=True)
    return bool(url)


def launcher(root, port, browser, test_http, *, background=False):
    releases = Releases(root)
    if background:
        browser = False
    if reopen(root,browser):
        return 0
    with ExitStack() as context:
        # The release launcher and the inner panel each own their own lock.
        try:
            context.enter_context(instance_lock(releases.data/"launcher-lock"))
        except InstanceBusy:
            if not browser:
                return 0
            wait_until = time.monotonic()+12
            while time.monotonic()<wait_until:
                if reopen(root,True):
                    return 0
                time.sleep(0.25)
            raise ValueError("执行端正在启动或切换版本，请稍后再次打开")
        bundled_paths()
        log = None
        if background:
            log_path = log_directory(root)/"startup.log"
            if log_path.is_file() and log_path.stat().st_size >= 1024*1024:
                log_path.replace(log_path.with_suffix(".log.1"))
            log = context.enter_context(log_path.open("a",encoding="utf-8",buffering=1))
            if sys.platform=="darwin":
                os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:"+os.environ.get("PATH","")
        while True:
            state = releases.state()
            selected = state["pending"] or state["active"]
            pending = state["pending"] is not None
            try:
                project = releases.directory(selected)
                python = executable(project) if selected["commit"] else Path(sys.executable)
                port, code = launch_panel(project,python,root,port,browser,test_http,releases,selected,state,log)
            except (ValueError,OSError) as exc:
                if log:
                    print(str(exc),file=log,flush=True)
                if not pending:
                    raise
                state.update(pending=None, error=f"新版启动失败，已恢复原版 · {exc}")
                releases.write(state)
                continue
            if code != RESTART_FOR_UPDATE:
                return code
            browser = False


def bundled_paths():
    runtime = Path(sys.executable).parent.parent.parent
    if not (runtime/"installed.txt").is_file():
        return
    uv = runtime/("uv.exe" if os.name=="nt" else "uv")
    if uv.is_file():
        os.environ.setdefault("FILM_WORKER_UV",str(uv))
    if (runtime/"tools").is_dir():
        os.environ.setdefault("FILM_WORKER_BUNDLED_TOOLS",str(runtime/"tools"))


def launch_panel(project, python, root, port, browser, test_http, releases, selected, state, log=None):
    run_id = uuid.uuid4().hex
    handshake = releases.data / "launch" / run_id
    handshake.mkdir(parents=True)
    env = dict(os.environ, FILM_WORKER_LAUNCH=str(handshake), FILM_WORKER_RELEASE=selected["commit"],
               FILM_WORKER_BOOTSTRAP=str(ROOT), FILM_WORKER_BOOTSTRAP_PYTHON=sys.executable)
    startup = [str(python), "-m", "film_worker", "--data", str(root)]
    if test_http:
        startup.append("--test-http")
    command = [*startup, "panel", "--port", str(port), "--no-browser"]
    child = None
    try:
        output = {"stdout":log,"stderr":subprocess.STDOUT} if log else {}
        if log and os.name=="nt":
            output["creationflags"] = subprocess.CREATE_NO_WINDOW
        child = subprocess.Popen(command,cwd=project,env=env,**output)
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
