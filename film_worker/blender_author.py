"""Start Blender for Codex scene authoring with a private background profile."""
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main(binary, arguments):
    end = arguments.index("--") if "--" in arguments else len(arguments)
    options = arguments[:end]
    if "-b" not in options and "--background" not in options:
        arguments = ["-b", *arguments]
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if "--console-crash-handler" not in options:
        help_text = subprocess.run([binary, "--help"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   timeout=10, creationflags=no_window).stdout
        if b"--console-crash-handler" in help_text:
            arguments = ["--console-crash-handler", *arguments]
    info = Path(binary).resolve().parent.parent / "Info.plist"
    if sys.platform == "darwin" and info.is_file():
        with info.open("rb") as source:
            version = plistlib.load(source).get("CFBundleShortVersionString", "")
        profile = Path.home() / "Library/Application Support/电影复刻工作台/后台制作" / version / "userpref.blend"
        if profile.parent.is_dir():
            if not profile.is_file():
                sys.exit(f"后台制作配置缺少 {profile.name}，请恢复 {profile} 后再启动 Blender。")
            if any(option.split("=", 1)[0] in ("--factory-startup", "-X", "--gpu-backend", "--debug-gpu-backend-no-fallback")
                   for option in options):
                sys.exit("此后台制作配置已选定图形后端，请去掉恢复出厂或探测 GPU 的启动参数后再运行。")
            with tempfile.TemporaryDirectory(prefix="movie-blender-config-") as config:
                shutil.copyfile(profile, Path(config) / "userpref.blend")
                return subprocess.run([binary, *arguments], env=dict(os.environ, BLENDER_USER_CONFIG=config)).returncode
    if os.name == "nt":
        return subprocess.run([binary, *arguments], creationflags=no_window).returncode
    os.execv(binary, [binary, *arguments])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2:]))
