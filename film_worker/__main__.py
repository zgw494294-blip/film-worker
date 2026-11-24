"""Command line for the film worker."""
import argparse
import json
import os
import sys
from pathlib import Path

from .settings import data_root, inspect_tools, load, resolve_tools
from .state import instance_lock


def main():
    parser = argparse.ArgumentParser(prog="film-worker")
    parser.add_argument("--data", type=Path, default=data_root(), help="本机数据目录")
    parser.add_argument("--test-http", action="store_true", help="允许访问本机 HTTP 模拟服务")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("run", help="运行后台执行服务")
    sub.add_parser("doctor", help="核对本机制作工具")
    panel = sub.add_parser("panel", help="打开本机控制界面")
    panel.add_argument("--port", type=int, default=0)
    panel.add_argument("--no-browser", action="store_true")
    launch = sub.add_parser("launch", help="运行可自动更新的本机面板")
    launch.add_argument("--port", type=int, default=0)
    launch.add_argument("--no-browser", action="store_true")
    launch.add_argument("--background", action="store_true", help="由登录任务运行，将诊断写入数据目录")
    login = sub.add_parser("login",help="管理当前帐号的登录启动")
    login.add_argument("setting",choices=("status","enable","disable"))
    sub.add_parser("self-test", help="校验本次安装的运行库和程序格式")
    child = sub.add_parser("child", help=argparse.SUPPRESS)
    child.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.action == "child":
        from .production import run_child
        return run_child(args.directory)
    root = args.data.expanduser().resolve()
    config = root / "config.json"
    try:
        if args.action == "doctor":
            tools = resolve_tools(load(config))
            print(json.dumps({"tools": tools, "versions": inspect_tools(tools)}, ensure_ascii=False, indent=2))
        elif args.action == "panel":
            from .panel import serve
            with instance_lock(root):
                return serve(root, args.port, not args.no_browser, test_http=args.test_http)
        elif args.action == "launch":
            from .releases import launcher
            return launcher(root,args.port,not args.no_browser,args.test_http,background=args.background)
        elif args.action == "login":
            from .startup import configure_login, login_enabled
            if args.setting != "status":
                configure_login(root,args.setting=="enable")
            print(json.dumps({"login_enabled":login_enabled(root)}))
        elif args.action == "self-test":
            from .selftest import check
            check(root)
        elif args.action == "run":
            from .scheduler import Scheduler
            from .secrets import read_token
            token = read_token(root)
            if not token:
                raise ValueError("请在本机控制面板保存建模 Token")
            with instance_lock(root):
                scheduler = Scheduler(load(config), root, token, test_http=args.test_http)
                try:
                    scheduler.run()
                except KeyboardInterrupt:
                    return 0
    except (ValueError, OSError) as exc:
        if sys.stderr is not None:
            print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
