"""Build Codex CLI options and read the selected-model evidence."""
import json
import os
import re
import subprocess
import tempfile

def options(binary, config, directory, *, sandbox, blender_config=None):
    command = [binary, "exec", "--skip-git-repo-check", "--sandbox", sandbox,
               "-c", 'approval_policy="never"', "-c", "project_doc_max_bytes=0",
               "-c", 'web_search="disabled"', "-c", "sandbox_workspace_write.network_access=false",
               "--disable", "hooks", "--disable", "memories", "--disable", "plugins",
               "--color", "never", "-C", str(directory)]
    if config.get("codex_model"):
        command += ["-m", config["codex_model"]]
    for field, key in (("reasoning_effort", "model_reasoning_effort"), ("codex_provider", "model_provider")):
        if config.get(field):
            command += ["-c", key+"="+json.dumps(config[field])]
    if blender_config:
        command += ["-c", "shell_environment_policy.set.BLENDER_USER_CONFIG="+json.dumps(blender_config)]
    return command


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
        env = {key: value for key, value in os.environ.items() if key != "FILM_WORKER_TOKEN"}
        try:
            output = subprocess.run(command, cwd=directory, check=True, timeout=100, capture_output=True,
                                    encoding="utf-8", errors="replace", env=env)
        except subprocess.SubprocessError as exc:
            raise ValueError("Codex 模型调用失败，请核对本机登录和模型服务配置") from exc
        observed = evidence(output.stderr[:32768])
        if "FILM_WORKER_OK" not in output.stdout:
            raise ValueError("Codex 未返回预期验证结果")
        if any(value and profile.get(field) and value != profile[field] for field, value in observed.items()):
            raise ValueError("实际模型或推理配置与本机设置不符")
        return {"reply":"FILM_WORKER_OK", "observed":observed}
