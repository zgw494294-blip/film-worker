"""Install the bundled runtime into the local per-node directory without network access."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


PROJECT = Path(__file__).resolve().parent


def root():
    home = Path.home()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Film Worker"
    return home / "Library/Application Support/Film Worker"


def python_target():
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    name = "macos" if platform.system() == "Darwin" else platform.system().lower()
    return f"{name}-{arch}"


def file_hash(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


@contextmanager
def install_lock(path):
    with path.open("a+b") as lock:
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def check_manifest(source, manifest):
    files = {}
    for file in manifest["files"]:
        relative = PurePosixPath(file["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in files:
            raise ValueError("运行环境清单含无效路径")
        files[relative.as_posix()] = file
    actual = {path.relative_to(source).as_posix() for path in source.rglob("*")
              if path.is_file() and path != source/"manifest.json" and not path.name.startswith(".")
              and "__pycache__" not in path.relative_to(source).parts}
    if actual != files.keys():
        raise ValueError("运行环境目录与清单不一致，请重新解压")
    for relative, file in files.items():
        path = source / relative
        if path.is_symlink() or path.stat().st_size != file["size"] or file_hash(path) != file["sha256"]:
            raise ValueError(f"随包文件校验失败 {relative}")


def install(data):
    source = PROJECT / "runtime"
    manifest_file = source / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    target = python_target()
    if manifest.get("schema") != 1 or manifest["target"] != target:
        raise ValueError("安装包与当前系统或架构不匹配")
    windows = os.name == "nt"
    pool = data / "runtime"
    pool.mkdir(parents=True, exist_ok=True)
    install_id = file_hash(manifest_file)
    region = pool / install_id[:24]
    uv_name = "uv.exe" if windows else "uv"
    base = region/"python"/("python.exe" if windows else "bin/python3.12")
    python = region/"env"/("Scripts/python.exe" if windows else "bin/python")
    installed_uv = region / uv_name
    marker = region/"installed.txt"
    env = dict(os.environ)
    env.update(UV_OFFLINE="1", UV_NO_CONFIG="1", UV_PYTHON_DOWNLOADS="never")
    env.pop("VIRTUAL_ENV", None)
    with install_lock(pool/"install.lock"):
        if (marker.is_file() and marker.read_text(encoding="ascii") == install_id and
                python.is_file() and installed_uv.is_file() and base.is_file()):
            return python, installed_uv
        check_manifest(source, manifest)
        if region.exists():
            shutil.rmtree(region)
        region.mkdir()
        try:
            shutil.copytree(source/"python", region/"python", ignore=shutil.ignore_patterns("._*", "__pycache__"))
            shutil.copy2(source/uv_name, installed_uv)
            if (source/"tools").exists():
                shutil.copytree(source/"tools", region/"tools", ignore=shutil.ignore_patterns("._*"))
            subprocess.run([str(installed_uv), "venv", "--offline", "--no-project", "--python", str(base),
                            str(region/"env")], check=True, cwd=PROJECT, env=env)
            subprocess.run([str(installed_uv), "pip", "sync", "--require-hashes", "--no-index", "--offline",
                            "--find-links", str(source/"wheels"), "--python", str(python), str(source/"wheels.txt")],
                           check=True, cwd=PROJECT, env=env)
            subprocess.run([str(python), "-m", "film_worker", "--data", str(data), "self-test"],
                           check=True, cwd=PROJECT, env=env)
            marker.write_text(install_id, encoding="ascii")
        except BaseException:
            shutil.rmtree(region, ignore_errors=True)
            raise
    return python, installed_uv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=root())
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--test-http", action="store_true")
    parser.add_argument("--install-only", action="store_true")
    args = parser.parse_args()
    data = args.data.expanduser().resolve()
    try:
        python, installed_uv = install(data)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"安装失败 · {exc}", file=sys.stderr)
        return 1
    if args.install_only:
        print(python, flush=True)
        return 0
    command = [str(python), "-m", "film_worker", "--data", str(data)]
    if args.test_http:
        command.append("--test-http")
    command += ["launch", "--port", str(args.port)]
    if args.no_browser:
        command.append("--no-browser")
    env = dict(os.environ, FILM_WORKER_UV=str(installed_uv), FILM_WORKER_BUNDLED_TOOLS=str(installed_uv.parent/"tools"))
    if os.name != "nt":
        os.execve(str(python), command, env)
    try:
        return subprocess.call(command, cwd=PROJECT, env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
