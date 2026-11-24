"""Node token in the macOS Keychain or Windows Credential Manager."""
import hashlib
import os
import subprocess


def label(root):
    return "film-worker/" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]


def read_token(root):
    inherited = os.environ.get("FILM_WORKER_TOKEN")
    if inherited:
        return inherited
    target = label(root)
    if os.name == "nt":
        import win32cred
        from pywintypes import error
        try:
            saved = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
        except error as exc:
            if exc.winerror == 1168:
                return ""
            raise
        return saved["CredentialBlob"].decode("utf-8")
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", target, "-w"],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def store_token(root, token):
    if not token.isascii() or not 1 <= len(token) <= 1024 or any(char.isspace() for char in token):
        raise ValueError("建模 Token 格式错误")
    target = label(root)
    if os.name == "nt":
        import win32cred
        win32cred.CredWrite(dict(Type=win32cred.CRED_TYPE_GENERIC, TargetName=target,
                                Persist=win32cred.CRED_PERSIST_LOCAL_MACHINE,
                                CredentialBlob=token.encode("utf-8")), 0)
    else:
        # Feed security's command interpreter via stdin so the token stays out of process arguments.
        import shlex
        line = 'add-generic-password -U -s ' + target + ' -a worker -w ' + shlex.quote(token)
        result = subprocess.run(["/usr/bin/security", "-i"], input=line+"\n", capture_output=True,
                                text=True, check=False)
        if result.returncode != 0 or read_token(root) != token:
            raise ValueError("macOS 钥匙串保存失败")
