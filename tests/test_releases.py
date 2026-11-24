"""Exercise signed releases, restart and rollback through the real local panel."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from film_worker.protocol import digest
from film_worker.releases import Releases, platform_key
from film_worker.settings import ROOT, atomic_json


@unittest.skipUnless(os.environ.get("FILM_WORKER_TEST_RELEASE") == "1", "requires Git, OpenSSH and uv")
class ReleaseTests(unittest.TestCase):
    def test_signature_activation_failure_and_manual_rollback(self):
        with tempfile.TemporaryDirectory(prefix="film-update-") as directory:
            root = Path(directory)
            remote = root/"remote repository"
            remote.mkdir()
            data = root/"data"
            data.mkdir()
            shutil.copytree(ROOT/"film_worker", remote/"film_worker", ignore=shutil.ignore_patterns("._*", "__pycache__"))
            for name in (".gitattributes", "pyproject.toml", "uv.lock"):
                shutil.copyfile(ROOT/name, remote/name)
            release_key = root/"release-key"
            self.run_command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(release_key)])
            signers = root/"allowed-signers"
            signers.write_text("publisher " + release_key.with_suffix(".pub").read_text())
            self.run_command(["git", "init", "-q", str(remote)])
            for field, value in (("user.name", "Publisher"), ("user.email", "publisher@example.test"),
                                 ("core.autocrlf", "true"),
                                 ("gpg.format", "ssh"), ("user.signingkey", str(release_key))):
                self.run_command(["git", "-C", str(remote), "config", field, value])
            config = dict(update_remote=str(remote), update_tag="stable", update_signers=str(signers))
            atomic_json(data/"config.json", config)
            self.publish(remote, 1, signed=False)
            with self.assertRaises(ValueError):
                Releases(data).stage(config)
            self.run_command(["git", "-C", str(Releases(data).repository), "config", "core.autocrlf", "true"])
            self.publish(remote, 2)

            with socket.socket() as candidate_port:
                candidate_port.bind(("127.0.0.1", 0))
                port = candidate_port.getsockname()[1]
            origin = f"http://127.0.0.1:{port}"
            log = data/"supervisor.log"
            with log.open("wb") as output:
                process = subprocess.Popen([sys.executable, "-m", "film_worker", "--data", str(data),
                                           "launch", "--no-browser", "--port", str(port)], cwd=ROOT,
                                           stdout=output, stderr=subprocess.STDOUT,
                                           env={**os.environ, "UV_NO_PROGRESS": "1"}, start_new_session=os.name != "nt")
                try:
                    self.wait_for(origin, lambda s: s["release"]["version"] == "local", process)
                    self.post(origin, "update")
                    installed = self.wait_for(origin, lambda s: s["release"]["version"] == "test-2", process)
                    self.assertTrue(installed["rollback_available"])

                    self.publish(remote, 3, broken=True)
                    self.post(origin, "update")
                    restored = self.wait_for(origin, lambda s: "新版启动失败" in s["update_status"], process)
                    self.assertEqual(restored["release"]["version"], "test-2")
                    self.assertEqual(Releases(data).state()["floor"], 3)

                    self.post(origin, "rollback")
                    self.wait_for(origin, lambda s: s["release"]["version"] == "local", process)
                    self.assertEqual(Releases(data).state()["floor"], 3)
                finally:
                    if process.poll() is None:
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                        else:
                            import signal
                            os.killpg(process.pid, signal.SIGINT)
                        process.wait(timeout=15)

    def run_command(self, args):
        subprocess.run(args, check=True, capture_output=True)

    def publish(self, repo, sequence, *, signed=True, broken=False):
        manifest = dict(version=f"test-{sequence}", sequence=sequence, platforms=[platform_key()], protocol=1,
                        state_format=1, lock_sha256=digest(repo/"uv.lock"))
        atomic_json(repo/"release.json", manifest)
        if broken:
            entry = repo/"film_worker/__main__.py"
            text = entry.read_text(encoding="utf-8")
            entry.write_text(text.replace('        elif args.action == "panel":',
                '        elif args.action == "panel":\n            return 5\n        elif args.action == "unused":'),
                encoding="utf-8")
        git = ["git", "-C", str(repo)]
        self.run_command([*git, "add", "."])
        self.run_command([*git, "commit", "-qm", str(sequence)])
        flag = "-s" if signed else "-a"
        self.run_command([*git, "tag", "-f", flag, "-m", f"release-{sequence}", "stable"])

    def post(self, origin, action):
        with urlopen(origin+"/api/config", timeout=3) as response:
            csrf = json.load(response)["csrf"]
        request = Request(origin+"/api/"+action, data=b"{}", headers={"Origin": origin, "X-Panel-Key": csrf})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def wait_for(self, origin, predicate, process):
        deadline = time.monotonic() + 45
        last = {}
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with urlopen(origin+"/api/status", timeout=2) as response:
                    last = json.load(response)
                if predicate(last):
                    return last
            except (URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(0.2)
        self.fail(f"Did not reach expected release state: {last}, exit={process.poll()}")
