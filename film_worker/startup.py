"""Per-user login registration, plus discovery of the running local panel."""
import hashlib
import json
import os
import platform
import plistlib
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from .settings import ROOT


def label(root):
    return "com.film-worker." + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:18]


def launch_command(root, *, windows):
    project = Path(os.environ.get("FILM_WORKER_BOOTSTRAP", ROOT))
    program = Path(os.environ.get("FILM_WORKER_BOOTSTRAP_PYTHON", sys.executable))
    if windows:
        pythonw = program.with_name("pythonw.exe")
        if pythonw.is_file():
            program = pythonw
    if not project.is_dir() or not program.is_file():
        raise ValueError("启动器位置已变化，请用安装目录里的启动文件重新打开")
    arguments = [str(program), "-X", "utf8", "-m", "film_worker", "--data", str(root), "launch", "--background"]
    return project, arguments


def log_directory(root):
    directory = root / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def agent_file(root, home=None):
    return (home or Path.home()) / "Library/LaunchAgents" / (label(root) + ".plist")


def configure_mac(root, enabled, *, home=None):
    file = agent_file(root, home)
    if not enabled:
        file.unlink(missing_ok=True)
        return
    project, args = launch_command(root, windows=False)
    logs = log_directory(root)
    data = dict(Label=label(root), ProgramArguments=args, WorkingDirectory=str(project),
                RunAtLoad=True, LimitLoadToSessionType="Aqua",
                StandardOutPath=str(logs / "launch-agent.log"), StandardErrorPath=str(logs / "launch-agent.log"))
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(".tmp")
    with temporary.open("wb") as out:
        plistlib.dump(data,out)
    temporary.replace(file)


def task_xml(sid, project, args):
    if not re.fullmatch(r"S-1-(?:[0-9]+-)+[0-9]+", sid):
        raise ValueError("无法确定当前 Windows 帐号")
    root = ET.Element("Task", {"xmlns":"http://schemas.microsoft.com/windows/2004/02/mit/task", "version":"1.2"})
    trigger = ET.SubElement(ET.SubElement(root,"Triggers"),"LogonTrigger")
    ET.SubElement(trigger,"Enabled").text = "true"
    ET.SubElement(trigger,"UserId").text = sid
    principal = ET.SubElement(ET.SubElement(root,"Principals"),"Principal",{"id":"Worker"})
    for name,value in (("UserId",sid),("LogonType","InteractiveToken"),("RunLevel","LeastPrivilege")):
        ET.SubElement(principal,name).text = value
    settings = ET.SubElement(root,"Settings")
    for name,value in (("MultipleInstancesPolicy","IgnoreNew"),("DisallowStartIfOnBatteries","false"),
                       ("StopIfGoingOnBatteries","false"),("StartWhenAvailable","true"),("ExecutionTimeLimit","PT0S")):
        ET.SubElement(settings,name).text = value
    action = ET.SubElement(ET.SubElement(root,"Actions",{"Context":"Worker"}),"Exec")
    ET.SubElement(action,"Command").text = args[0]
    ET.SubElement(action,"Arguments").text = subprocess.list2cmdline(args[1:])
    ET.SubElement(action,"WorkingDirectory").text = str(project)
    return ET.tostring(root,encoding="utf-16",xml_declaration=True)


def windows_command(*args, check=True):
    try:
        result = subprocess.run(args,capture_output=True,text=True,errors="replace",timeout=12,
                                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Windows 登录任务服务响应超时") from exc
    if check and result.returncode:
        raise ValueError(f"Windows 计划任务操作失败（{Path(args[0]).name}，退出码 {result.returncode}）")
    return result


def configure_windows(root, enabled):
    name = label(root)
    if not enabled:
        if login_enabled(root):
            windows_command("schtasks.exe","/Delete","/TN",name,"/F")
        return
    raw = windows_command("whoami.exe","/user","/fo","csv","/nh").stdout
    identifier = re.search(r"\bS-1-(?:[0-9]+-)+[0-9]+\b",raw)
    if not identifier:
        raise ValueError("无法确定当前 Windows 帐号")
    project, args = launch_command(root,windows=True)
    with tempfile.TemporaryDirectory(prefix="film-worker-task-") as directory:
        xml = Path(directory)/"task.xml"
        xml.write_bytes(task_xml(identifier[0],project,args))
        windows_command("schtasks.exe","/Create","/TN",name,"/XML",str(xml),"/F")


def login_enabled(root):
    system = platform.system()
    if system=="Darwin":
        return agent_file(root).is_file()
    if system=="Windows":
        return windows_command("schtasks.exe","/Query","/TN",label(root),check=False).returncode==0
    return False


def configure_login(root, enabled):
    if type(enabled) is not bool:
        raise ValueError("登录启动设置须为布尔值")
    system = platform.system()
    if system=="Darwin":
        return configure_mac(root,enabled)
    if system=="Windows":
        return configure_windows(root,enabled)
    raise ValueError("当前系统没有登录启动支持")


def endpoint_file(root):
    return root/"panel-instance.json"


def running_panel(root):
    try:
        endpoint = json.loads(endpoint_file(root).read_text(encoding="utf-8"))
        port, instance = endpoint["port"],endpoint["instance"]
        if type(port) is not int or not 1 <= port <= 65535 or not re.fullmatch(r"[0-9a-f]{32}",instance):
            return None
        url = f"http://127.0.0.1:{port}"
        with build_opener(ProxyHandler({})).open(url+"/api/instance",timeout=1) as response:
            return url if json.load(response)["instance"]==instance else None
    except (URLError,OSError,KeyError,TypeError,ValueError):
        return None
