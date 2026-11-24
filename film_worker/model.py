"""Build Codex CLI options and read the selected-model evidence."""
import json
import re
import subprocess
import tempfile

from .settings import object_hash


MODEL_FIELDS = ("codex_provider", "codex_model", "reasoning_effort")


def options(binary, profile, directory, *, sandbox):
    return [binary, "exec", "--skip-git-repo-check", "--sandbox", sandbox,
            "-c", 'approval_policy="never"', "-m", profile["codex_model"],
            "-c", "model_reasoning_effort="+json.dumps(profile["reasoning_effort"]),
            "-c", "model_provider="+json.dumps(profile["codex_provider"]),
            "--color", "never", "-C", str(directory)]


def evidence(header):
    result = {}
    fields = dict(codex_model="model", codex_provider="provider", reasoning_effort="reasoning effort")
    for field, name in fields.items():
        value = re.search(r"^" + name + r":\s*([^\r\n]+)", header, re.M)
        result[field] = value.group(1).strip() if value else None
    return result


def test(binary, profile):
    with tempfile.TemporaryDirectory(prefix="film-model-test-") as directory:
        command = [*options(binary, profile, directory, sandbox="read-only"), "只回答 FILM_WORKER_OK"]
        try:
            output = subprocess.run(command, cwd=directory, check=True, timeout=100, capture_output=True,
                                    encoding="utf-8", errors="replace")
        except subprocess.SubprocessError as exc:
            raise ValueError("Codex 模型调用失败，请核对本机登录和模型服务配置") from exc
        observed = evidence(output.stderr[:32768])
        if "FILM_WORKER_OK" not in output.stdout:
            raise ValueError("Codex 未返回预期验证结果")
        if any(value is not None and value != profile[field] for field, value in observed.items()):
            raise ValueError("实际模型或推理配置与制作档案不符")
        return {"reply":"FILM_WORKER_OK", "observed":observed}


def draft(original, model):
    profile = {**original}
    for field in MODEL_FIELDS:
        value = model[field]
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value):
            raise ValueError(f"请填写有效的 {field}")
        profile[field] = value
    profile.pop("profile_hash", None)
    profile["profile_hash"] = object_hash(profile)
    return profile
