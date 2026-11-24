"""Local configuration and capability discovery."""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

import av
import PIL
import rfc8785

from .protocol import opaque


ROOT = Path(__file__).resolve().parent.parent
KINDS = ["prepare", "revise", "finalize"]

DEFAULTS = {
    "server": "",
    "node_key": "",
    "display_name": platform.node(),
    "slots": 1,
    "model_slots": 1,
    "render_slots": 1,
    "transfer_slots": 1,
    "cpu_threads": 4,
    "disk_reserve_gib": 8,
    "codex": "",
    "blender": "",
    "ffmpeg": "",
    "ffprobe": "",
    "render_backend": "auto",
    "profiles": [],
    "update_remote": "",
    "update_tag": "stable",
    "update_signers": "",
    "update_interval_minutes": 30,
    "update_window_start": "",
    "update_window_end": "",
    "auto_update": False,
}


def data_root():
    home = Path.home()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Film Worker"
    return home / "Library/Application Support/Film Worker"


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        out.flush()
        os.fsync(out.fileno())
    tmp.replace(path)


def object_hash(value):
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def bundled_release():
    bootstrap = Path(os.environ.get("FILM_WORKER_BOOTSTRAP", ROOT))
    release = bootstrap / "release.json"
    if (bootstrap / "runtime/build.json").is_file() and release.is_file():
        return bootstrap, json.loads(release.read_text(encoding="utf-8"))
    return None


def load(path):
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if bundled := bundled_release():
        bootstrap, manifest = bundled
        signers = bootstrap / "release.allowed_signers"
        if (remote := manifest.get("update_remote")) and signers.is_file():
            settings = {"update_remote": remote, "update_signers": str(signers), "auto_update": True, **settings}
    return validate_config(settings)


def validate_config(settings):
    config = {**DEFAULTS, **{k: v for k,v in settings.items() if k in DEFAULTS}}
    for field in ("slots", "model_slots", "render_slots", "transfer_slots", "cpu_threads", "disk_reserve_gib"):
        if type(config[field]) is not int or not 1 <= config[field] <= 128:
            raise ValueError(f"{field} 须为 1–128 的整数")
    interval = config["update_interval_minutes"]
    if type(interval) is not int or not 1 <= interval <= 1440 or type(config["auto_update"]) is not bool:
        raise ValueError("更新周期须为 1–1440 分钟")
    for field in ("server", "node_key", "display_name", "codex", "blender", "ffmpeg", "ffprobe",
                  "update_remote", "update_tag", "update_signers", "update_window_start", "update_window_end"):
        if not isinstance(config[field], str) or "\0" in config[field]:
            raise ValueError(f"{field} 格式错误")
    start,end = config["update_window_start"],config["update_window_end"]
    if (start or end) and (start==end or not all(re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", item)
                                               for item in (start,end))):
        raise ValueError("自动更新时段需填写不同的开始、结束时间（HH:MM）")
    if not isinstance(config["profiles"], list) or any(not isinstance(p, dict) for p in config["profiles"]):
        raise ValueError("制作档案应为 JSON 数组")
    if config["render_backend"] not in {"auto", "cpu", "metal", "cuda", "optix", "hip", "oneapi"}:
        raise ValueError("未知 Blender 计算后端")
    return config


def windows_desktop_codex():
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return None
    script = (
        "$ProgressPreference='SilentlyContinue'; "
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$app=Get-AppxPackage -Name OpenAI.Codex -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($app) { [Console]::WriteLine((Join-Path $app.InstallLocation 'app\\resources\\codex.exe')) }"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, check=True, timeout=10, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        path = Path(result.stdout.strip())
        if path.is_absolute() and path.is_file():
            return str(path)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    return None


def resolve_tools(config):
    mac = {
        "blender": ["/Applications/Blender.app/Contents/MacOS/Blender"],
        "codex": ["/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"],
    }
    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    roaming = Path(os.environ.get("APPDATA", str(Path.home()/"AppData/Roaming")))
    npm_roots = [roaming/"npm"]
    shim = shutil.which("codex.cmd") if os.name == "nt" else None
    if shim:
        npm_roots.append(Path(shim).parent)
    npm_candidates = []
    if os.name == "nt":
        machine = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        target = "aarch64" if machine == "arm64" else "x86_64"
        for path in npm_roots:
            vendor = path/("node_modules/@openai/codex/node_modules/@openai/codex-win32-"+machine)
            vendor = vendor/f"vendor/{target}-pc-windows-msvc"
            npm_candidates.extend([vendor/"bin/codex.exe",vendor/"codex/codex.exe"])
    windows = {
        "codex": [os.getenv("CODEX_CLI_PATH"), *npm_candidates],
        "blender": sorted((program_files / "Blender Foundation").glob("Blender */blender.exe"), reverse=True),
    }
    sites = windows if os.name == "nt" else mac
    tools = {}
    bundled = os.environ.get("FILM_WORKER_BUNDLED_TOOLS")
    for name in ("codex", "blender", "ffmpeg", "ffprobe"):
        filename = name + ".exe" if os.name == "nt" else name
        packaged = Path(bundled) / filename if bundled else None
        candidates = [config[name]] if config[name] else [packaged, shutil.which(name), *sites.get(name, [])]
        for candidate in candidates:
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                if os.name == "nt" and Path(candidate).suffix.lower() != ".exe":
                    continue
                tools[name] = str(Path(candidate).resolve())
                break
        if name == "codex" and os.name == "nt" and not config[name] and name not in tools:
            if desktop := windows_desktop_codex():
                tools[name] = desktop
        if name not in tools:
            raise ValueError(f"找不到 {name}，请设置可执行文件路径")
    return tools


def version(path, pattern, flag="--version"):
    args = [path, flag]
    try:
        result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=20, check=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.SubprocessError as exc:
        raise ValueError(f"{Path(path).name} 版本检测失败") from exc
    match = re.search(pattern, result.stdout)
    if not match:
        raise ValueError(f"无法读取版本 {Path(path).name}")
    return match.group(1)


def local_build():
    active = os.environ.get("FILM_WORKER_RELEASE")
    if not active:
        manifest = ROOT / "runtime" / "build.json"
        if manifest.is_file():
            active = json.loads(manifest.read_text(encoding="utf-8"))["commit"]
        elif (ROOT / ".git").exists():
            try:
                result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
                                        capture_output=True, text=True, timeout=5, check=True)
                active = result.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return None
    return active if active and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", active) else None


def inspect_tools(tools):
    ffmpeg = version(tools["ffmpeg"], r"ffmpeg version ([^\s]+)", "-version")
    probe = version(tools["ffprobe"], r"ffprobe version ([^\s]+)", "-version")
    build = version(tools["blender"], r"Blender ([^\s]+)")
    cli = version(tools["codex"], r"codex-cli ([^\s]+)")
    if probe != ffmpeg:
        raise ValueError("FFmpeg 和 FFprobe 版本必须一致")
    result = {"blender": build, "ffmpeg": ffmpeg, "codex_cli": cli,
              "decoder_id": f"rgb8-pillow{PIL.__version__}-av{av.__version__}/1"}
    if commit := local_build():
        result["runner_commit"] = commit
    return result


def environment(config, versions, profile, evidence="configured", actual_backend=None):
    mac = platform.system() == "Darwin"
    operating_system = "macOS " + platform.mac_ver()[0] if mac else "Windows " + platform.version()
    backend = config["render_backend"]
    if backend == "auto":
        backend = "metal" if mac and platform.machine() == "arm64" else "cpu"
    source = profile if evidence != "unknown" else {}
    return dict(os=operating_system, arch=platform.machine().lower(), gpu="auto", render_backend=actual_backend or backend,
                compatibility={**profile["compatibility"], **versions}, model_evidence=evidence,
                **{field: source.get(field) for field in ("codex_model", "codex_provider", "reasoning_effort")})


def profile_matches(profile, versions):
    for field in ("codex_model", "codex_provider", "reasoning_effort"):
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", str(profile.get(field, ""))):
            raise ValueError(f"制作档案的 {field} 格式错误")
    advertised = profile["profile_hash"]
    value = {k: v for k, v in profile.items() if k != "profile_hash"}
    if object_hash(value) != advertised:
        raise ValueError("制作档案摘要错误")
    return all(profile["compatibility"][name] == value for name, value in versions.items())


def registration(config, instance_id, versions):
    all_profiles = config["profiles"]
    profiles = [p for p in all_profiles if profile_matches(p, versions)]
    if not profiles:
        if not all_profiles:
            raise ValueError("尚未保存管理端签发的制作档案")
        first = all_profiles[0]["compatibility"]
        fields = [f"{name} 要求 {first[name]}，本机 {value}" for name,value in versions.items() if first[name] != value]
        raise ValueError("制作档案不匹配 · " + "；".join(fields))
    opaque(config["node_key"])
    env = environment(config, versions, profiles[0])
    available = sorted({"cpu", env["render_backend"]})
    work_kinds = KINDS if any(p.get("production_policy") != "human_required" for p in profiles) else ["finalize"]
    return dict(node_key=config["node_key"], instance_id=instance_id, display_name=config["display_name"],
                capabilities=dict(work_kinds=work_kinds, profile_hashes=[p["profile_hash"] for p in profiles],
                                  slots=config["slots"], max_frame_width=max(p["final_width"] for p in profiles),
                                  render_backends=available, environment=env))
