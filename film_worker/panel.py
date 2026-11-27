"""Loopback-only control panel."""
import json
import os
import secrets
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import model, startup
from .releases import RESTART_FOR_UPDATE, Releases
from .scheduler import Scheduler
from .secrets import read_token, store_oss_credentials, store_token
from .settings import atomic_json, inspect_tools, load, resolve_tools, validate_config


WEB = Path(__file__).with_name("web")


def update_allowed(config, when):
    start,end = config["update_window_start"],config["update_window_end"]
    if not start:
        return True
    clock = when.strftime("%H:%M")
    if start<end:
        return start<=clock<end
    return clock>=start or clock<end


class Panel(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root, port, test_http):
        super().__init__(("127.0.0.1", port), Handler)
        self.root = root
        self.config = root / "config.json"
        self.scheduler = None
        self.runner = None
        self.lock = threading.Lock()
        self.csrf = secrets.token_urlsafe()
        self.instance = secrets.token_hex(16)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.test_http = test_http
        self.start_error = None
        self.updater = Releases(root)
        self.update_lock = threading.Lock()
        self.update_display = "待检查"
        self.update_halt = threading.Event()
        self.managed = bool(os.getenv("FILM_WORKER_LAUNCH"))
        self.restart = False

    def start(self):
        if self.runner and self.runner.is_alive():
            return
        token = read_token(self.root)
        if not token:
            raise ValueError("请先保存建模 Token")
        self.scheduler = Scheduler(load(self.config), self.root, token, test_http=self.test_http)
        self.runner = threading.Thread(target=self.scheduler.run, daemon=True)
        self.runner.start()

    def summary(self):
        release = self.updater.state()
        detail = dict(managed=self.managed, release=release["active"], update_status=release["error"] or self.update_display,
                      rollback_available=release["previous"] is not None)
        if self.scheduler and self.runner and self.runner.is_alive():
            return {**self.scheduler.summary(), **detail}
        return dict(connection="stopped", active=[], draining=False, detail=self.start_error, **detail)

    def update(self, *, rollback=False):
        if not self.managed:
            raise ValueError("请通过启动器打开面板以启用升级")
        if not self.update_lock.acquire(blocking=False):
            raise ValueError("正在进行另一个更新操作")
        self.update_display = "准备回退" if rollback else "检查签名和依赖"
        threading.Thread(target=self.perform_update, kwargs={"rollback": rollback}, daemon=True).start()

    def perform_update(self, *, rollback):
        try:
            release = self.updater.state()
            candidate = release["previous"] if rollback else self.updater.stage(load(self.config))
            if candidate is None:
                self.update_display = "没有可用的更新"
                return
            with self.lock:
                runner = self.runner
                if runner and runner.is_alive():
                    self.scheduler.drain.set()
                    self.scheduler.stop_after_drain.set()
            self.update_display = "正在等待当前工单提交"
            if runner:
                runner.join()
            if self.update_halt.is_set():
                return
            self.updater.queue(candidate, rollback=rollback)
            self.restart = True
            self.shutdown()
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.update_display = str(exc)
        finally:
            self.update_lock.release()

    def poll_updates(self):
        last_interval = 0
        next_check = 0
        while not self.update_halt.wait(1):
            try:
                config = load(self.config)
                interval = config["update_interval_minutes"] * 60
                if interval != last_interval:
                    next_check = time.monotonic() + interval
                    last_interval = interval
                if (config["auto_update"] and config["update_remote"] and
                        time.monotonic() >= next_check and not self.update_lock.locked() and
                        update_allowed(config,datetime.now())):
                    self.update()
                    next_check = time.monotonic() + interval
            except (ValueError, OSError):
                next_check = time.monotonic() + 60


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, body, mime="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def permitted(self):
        return self.headers.get("Host") == self.server.origin.removeprefix("http://")

    def do_GET(self):
        if not self.permitted():
            return self.reply(403, {"error": "请求来源无效"})
        path = urlsplit(self.path).path
        if path == "/api/instance":
            return self.reply(200, {"instance":self.server.instance})
        if path == "/api/login":
            try:
                return self.reply(200, {"enabled":startup.login_enabled(self.server.root)})
            except (ValueError,OSError) as exc:
                return self.reply(400, {"error":str(exc)})
        if path == "/api/status":
            return self.reply(200, self.server.summary())
        if path == "/api/config":
            return self.reply(200, {"config": load(self.server.config), "csrf": self.server.csrf})
        assets = {"/": ("index.html", "text/html"), "/panel.css": ("panel.css", "text/css"),
                  "/panel.js": ("panel.js", "text/javascript")}
        if path not in assets:
            return self.reply(404, {"error": "页面不存在"})
        file, mime = assets[path]
        self.reply(200, (WEB / file).read_bytes(), mime+"; charset=utf-8")

    def do_POST(self):
        if (not self.permitted() or self.headers.get("Origin") != self.server.origin or
                self.headers.get("X-Panel-Key") != self.server.csrf):
            return self.reply(403, {"error": "请求来源无效"})
        node = self.server
        try:
            count = int(self.headers.get("Content-Length", "0"))
            if not 0 < count <= 1024 * 1024:
                raise ValueError("配置数据过大")
            payload = json.loads(self.rfile.read(count))
            if self.path == "/api/login":
                if not node.managed:
                    raise ValueError("请通过启动器运行后设置登录启动")
                with node.lock:
                    startup.configure_login(node.root,payload.get("enabled"))
                    return self.reply(200, {"enabled":startup.login_enabled(node.root)})
            if self.path == "/api/model-test":
                if node.runner and node.runner.is_alive():
                    raise ValueError("请先暂停领单并停止节点，再验证模型")
                config = load(node.config)
                answer = model.test(resolve_tools(config)["codex"], config)
                return self.reply(200,answer)
            with node.lock:
                if self.path == "/api/config":
                    if node.runner and node.runner.is_alive():
                        raise ValueError("请先暂停领单，等待在途工单完成，再停止节点并保存")
                    config = validate_config(payload["config"])
                    if payload.get("oss_key_id") or payload.get("oss_key_secret"):
                        store_oss_credentials(node.root, payload.get("oss_key_id"), payload.get("oss_key_secret"))
                    if payload.get("token"):
                        store_token(node.root, payload["token"])
                    atomic_json(node.config, config)
                    return self.reply(200, {"saved": True})
                if self.path == "/api/probe":
                    versions = inspect_tools(resolve_tools(load(node.config)))
                    return self.reply(200, {"versions": versions})
                if self.path in ("/api/update", "/api/rollback"):
                    node.update(rollback=self.path == "/api/rollback")
                elif self.path == "/api/start":
                    if node.update_lock.locked():
                        raise ValueError("更新正在进行，请稍候")
                    node.start()
                    node.start_error = None
                elif self.path in ("/api/drain", "/api/resume", "/api/stop"):
                    if node.scheduler and node.runner.is_alive():
                        if self.path == "/api/drain":
                            node.scheduler.drain.set()
                        elif self.path == "/api/resume":
                            node.scheduler.drain.clear()
                        else:
                            if node.scheduler.summary()["active"] or not node.scheduler.drain.is_set():
                                raise ValueError("请先暂停领单并等待在途工单完成")
                            node.scheduler.stop.set()
                else:
                    return self.reply(404, {"error": "未知操作"})
            self.reply(200, node.summary())
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.reply(400, {"error": str(exc)})


def serve(root, port, browser, *, test_http=False):
    with Panel(root, port, test_http) as server:
        if server.managed:
            # Confirm runtime, data format and panel bind before the launcher activates this release.
            from .selftest import check
            check(root)
            session = Path(os.environ["FILM_WORKER_LAUNCH"])
            atomic_json(session / "ready.json", {"port": server.server_port})
            deadline = time.monotonic() + 45
            while not (session / "activate.json").exists():
                if time.monotonic() >= deadline:
                    raise ValueError("启动器确认超时")
                time.sleep(0.1)
        if server.config.is_file():
            try:
                server.start()
            except (ValueError, OSError) as exc:
                server.start_error = str(exc)
        threading.Thread(target=server.poll_updates, daemon=True).start()
        marker = startup.endpoint_file(root)
        atomic_json(marker,{"port":server.server_port,"instance":server.instance})
        print(server.origin, flush=True)
        if browser:
            webbrowser.open(server.origin)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.update_halt.set()
            marker.unlink(missing_ok=True)
            if server.runner:
                server.scheduler.stop.set()
                server.runner.join()
        return RESTART_FOR_UPDATE if server.restart else 0
