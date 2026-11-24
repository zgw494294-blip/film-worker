"""Smoke-test a distribution on its native operating system."""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def verify(path):
    expected = path.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()[0]
    if digest(path) != expected:
        raise ValueError("发布包摘要不符")
    with tempfile.TemporaryDirectory(prefix="film-worker-install-") as temp:
        root = Path(temp)
        with zipfile.ZipFile(path) as archive:
            archive.extractall(root)
            if os.name != "nt":
                for info in archive.infolist():
                    mode = (info.external_attr>>16) & 0o777
                    if mode:
                        (root/info.filename).chmod(mode)
        project = root/"film-worker"
        data = root/"测试 数据"
        interpreter = project/"runtime"/("python/python.exe" if os.name=="nt" else "python/bin/python3.12")
        setup = [str(interpreter),"-X","utf8","-B","install_runtime.py","--data",str(data)]
        env = {**os.environ, "PYTHONUTF8":"1", "UV_OFFLINE":"1", "HTTPS_PROXY":"http://127.0.0.1:1",
               "HTTP_PROXY":"http://127.0.0.1:1", "NO_PROXY":"127.0.0.1"}
        out = subprocess.run([*setup,"--install-only"],cwd=project,env=env,
                             capture_output=True,encoding="utf-8",check=True,timeout=80)
        python = Path(out.stdout.strip().splitlines()[-1])
        marker = next((data/"runtime").glob("*/installed.txt"))
        verify_media_tools(marker.parent / "tools", root,env)
        since = marker.stat().st_mtime_ns
        out = subprocess.run([*setup,"--install-only"],cwd=project,env=env,
                             capture_output=True,encoding="utf-8",check=True,timeout=15)
        if marker.stat().st_mtime_ns != since or Path(out.stdout.strip()) != python:
            raise ValueError("第二次安装未复用第一次下载结果")
        with (root/"launch.log").open("w+", encoding="utf-8") as log:
            child = subprocess.Popen([*setup,"--no-browser"],cwd=project,env=env,stdout=log,
                                     stderr=subprocess.STDOUT,start_new_session=os.name!="nt")
            try:
                wait_for_panel(child, log)
            finally:
                if child.poll() is None:
                    if os.name=="nt":
                        subprocess.run(["taskkill","/f","/t","/pid",str(child.pid)],capture_output=True,timeout=15)
                    else:
                        os.killpg(child.pid,signal.SIGINT)
                    child.wait(timeout=15)


def verify_media_tools(tools, temp, env):
    suffix = ".exe" if os.name=="nt" else ""
    command = tools/("ffmpeg"+suffix)
    probe = tools/("ffprobe"+suffix)
    if not command.is_file() and not probe.is_file():
        return
    for program in (command,probe):
        line = subprocess.check_output([str(program),"-version"],env=env,encoding="utf-8",timeout=12).splitlines()[0]
        if " version 9.0.2 " not in line:
            raise ValueError("安装包中的媒体工具版本不匹配")
    clip = temp/"验收短片.mp4"
    subprocess.run([str(command),"-v","error","-f","lavfi","-i","testsrc2=size=96x64:rate=24",
                    "-frames:v","4","-c:v","mpeg4",str(clip)],env=env,check=True,timeout=15)
    info = json.loads(subprocess.check_output([str(probe),"-v","error","-show_frames","-of","json",str(clip)],
                                             env=env,encoding="utf-8",timeout=15))
    if len(info["frames"]) != 4 or not all(frame["width"]==96 and frame["height"]==64 for frame in info["frames"]):
        raise ValueError("随包媒体工具未按原画幅逐帧解码")


def wait_for_panel(child, log):
    limit = time.monotonic()+35
    while child.poll() is None and time.monotonic()<limit:
        log.seek(0)
        url = next((line.strip() for line in log if line.startswith("http://127.0.0.1:")),None)
        if url:
            try:
                with urlopen(url+"/api/status",timeout=2) as response:
                    if json.load(response)["managed"] is True:
                        return
            except URLError:
                pass
        time.sleep(0.25)
    log.seek(0)
    raise RuntimeError("安装包面板启动失败\n"+log.read()[-2000:])


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip",type=Path)
    verify(parser.parse_args().zip)
    print("verified offline install, cache reuse and control panel")
