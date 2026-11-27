"""Node token in the macOS Keychain or Windows Credential Manager."""
import hashlib
import json
import os
import subprocess


def label(root):
    return "film-worker/" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]


def read_token(root):
    inherited = os.environ.get("FILM_WORKER_TOKEN")
    if inherited:
        return inherited
    return read_stored(label(root))


def read_stored(target):
    if os.name == "nt":
        import win32cred
        from pywintypes import error
        try:
            saved = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
        except error as exc:
            if exc.winerror == 1168:
                return ""
            raise windows_credential_error(exc, "读取") from exc
        return saved["CredentialBlob"].decode("utf-16-le")
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", target, "-w"],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def store_token(root, token):
    if not token.isascii() or not 1 <= len(token) <= 1024 or any(char.isspace() for char in token):
        raise ValueError("建模 Token 格式错误")
    store_secret(label(root), token)


OSS_ENV = ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_SESSION_TOKEN")


def oss_credentials(root):
    access_id, secret, session = (os.environ.get(name, "") for name in OSS_ENV)
    if access_id or secret or session:
        if not access_id or not secret:
            raise ValueError("请同时提供 OSS AccessKey ID 和 Secret")
        return access_id, secret, session or None
    stored = read_stored(label(root) + "/oss")
    if not stored:
        raise ValueError("此工单需要 OSS 自动交付，请先在执行端保存 OSS AccessKey")
    return (*json.loads(stored), None)


def store_oss_credentials(root, access_id, secret):
    if any(not isinstance(value, str) or not value.isascii() or not 1 <= len(value) <= 256
           or any(not 33 <= ord(char) <= 126 for char in value) for value in (access_id, secret)):
        raise ValueError("请同时填写有效的 OSS AccessKey ID 和 Secret")
    store_secret(label(root) + "/oss", json.dumps([access_id, secret], separators=(",", ":")))


def store_secret(target, value):
    if os.name == "nt":
        import win32cred
        from pywintypes import error
        try:
            win32cred.CredWrite(dict(Type=win32cred.CRED_TYPE_GENERIC, TargetName=target,
                                    Persist=win32cred.CRED_PERSIST_LOCAL_MACHINE,
                                    CredentialBlob=value), 0)
        except error as exc:
            raise windows_credential_error(exc, "保存") from exc
    else:
        # Feed security's command interpreter via stdin so the token stays out of process arguments.
        import shlex
        line = 'add-generic-password -U -s ' + target + ' -a worker -w ' + shlex.quote(value)
        result = subprocess.run(["/usr/bin/security", "-i"], input=line+"\n", capture_output=True,
                                text=True, check=False)
        if result.returncode != 0 or read_stored(target) != value:
            raise ValueError("macOS 钥匙串保存失败")


def windows_credential_error(exc, operation):
    if exc.winerror == 1312:
        return ValueError("当前 Windows 登录会话无法访问凭据管理器。请在桌面会话配置建模 Token，"
                          "或在部署环境设置 FILM_WORKER_TOKEN")
    return ValueError(f"Windows 凭据管理器{operation}失败（错误 {exc.winerror}）")
