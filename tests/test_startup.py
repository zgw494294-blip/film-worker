"""Validate login registration files and the actual background launcher."""
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from film_worker import startup
from film_worker.panel import update_allowed
from film_worker.processes import ProcessTree
from film_worker.settings import DEFAULTS, ROOT, validate_config


class StartupTests(unittest.TestCase):
    def test_macos_login_registration_is_scoped_and_reversible(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home/"节点 数据"
            startup.configure_mac(root,True,home=home)
            plist = startup.agent_file(root,home)
            with plist.open("rb") as payload:
                data = plistlib.load(payload)
            self.assertEqual(data["WorkingDirectory"],str(ROOT))
            self.assertEqual(data["ProgramArguments"][-3:],[str(root),"launch","--background"])
            self.assertTrue(data["RunAtLoad"])
            self.assertIn(str(root),data["StandardOutPath"])
            self.assertNotIn("FILM_WORKER_TOKEN",data)
            if sys.platform=="darwin":
                subprocess.run(["/usr/bin/plutil","-lint",str(plist)],check=True,capture_output=True)
            startup.configure_mac(root,False,home=home)
            self.assertFalse(plist.exists())

    def test_windows_task_uses_current_sid_and_preserves_quoted_paths(self):
        sid = "S-1-5-21-123-456-789-1001"
        xml = startup.task_xml(sid,Path("C:/安装 目录"),["C:/执行端/pythonw.exe","--data","C:/生产 数据","launch","--background"])
        task = ET.fromstring(xml)
        ns = {"t":"http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(task.findtext("t:Triggers/t:LogonTrigger/t:UserId",namespaces=ns),sid)
        self.assertEqual(task.findtext("t:Principals/t:Principal/t:LogonType",namespaces=ns),"InteractiveToken")
        self.assertEqual(task.findtext("t:Settings/t:ExecutionTimeLimit",namespaces=ns),"PT0S")
        self.assertIn('"C:/生产 数据"',task.findtext("t:Actions/t:Exec/t:Arguments",namespaces=ns))
        with self.assertRaises(ValueError):
            startup.task_xml("bad-sid",Path("C:/"),["pythonw.exe"])

    @unittest.skipUnless(os.name=="nt" and os.getenv("FILM_WORKER_TEST_LOGIN")=="1","run OS task registration on a disposable Windows runner")
    def test_real_windows_login_task_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/"计划任务 测试"
            self.assertFalse(startup.login_enabled(root))
            try:
                startup.configure_login(root,True)
                self.assertTrue(startup.login_enabled(root))
            finally:
                startup.configure_login(root,False)
            self.assertFalse(startup.login_enabled(root))

    def test_background_launch_accepts_repeat_start_without_creating_another_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/"值守 节点"
            env = dict(os.environ,PYTHONUTF8="1")
            for field in ("FILM_WORKER_LAUNCH","FILM_WORKER_BOOTSTRAP","FILM_WORKER_BOOTSTRAP_PYTHON","FILM_WORKER_RELEASE"):
                env.pop(field,None)
            argv = [sys.executable,"-m","film_worker","--data",str(root),"launch","--background"]
            with (Path(directory)/"launcher.log").open("wb") as output:
                tree = ProcessTree(argv,cwd=ROOT,env=env,log=output)
            child = tree.child
            log = root/"logs/startup.log"
            try:
                end = time.monotonic()+55
                while child.poll() is None and not startup.running_panel(root) and time.monotonic()<end:
                    time.sleep(0.15)
                endpoint = startup.running_panel(root)
                self.assertTrue(endpoint,log.read_text(encoding="utf-8") if log.exists() else "no log")
                with startup.endpoint_file(root).open(encoding="utf-8") as handle:
                    address = json.load(handle)
                subprocess.run(argv,cwd=ROOT,env=env,timeout=12,check=True)
                self.assertEqual(startup.running_panel(root),endpoint)
                with startup.endpoint_file(root).open(encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle),address)
                self.assertIn(endpoint,log.read_text(encoding="utf-8"))
            finally:
                tree.close()
                if os.name == "nt":
                    # Windows may finish closing the terminated panel's log asynchronously.
                    deadline = time.monotonic()+5
                    while True:
                        try:
                            log.unlink(missing_ok=True)
                            break
                        except PermissionError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.05)

    def test_update_window_supports_midnight_and_legacy_all_day(self):
        def when(hour,minute):
            return datetime(2026,11,24,hour,minute)

        self.assertTrue(update_allowed(DEFAULTS,when(12,0)))
        night = validate_config(dict(update_window_start="23:00",update_window_end="05:00"))
        for hour,minute,expected in ((22,59,False),(23,0,True),(0,0,True),(4,59,True),(5,0,False)):
            self.assertEqual(update_allowed(night,when(hour,minute)),expected)
        with self.assertRaises(ValueError):
            validate_config(dict(update_window_start="02:00"))
