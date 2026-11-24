"""Node supervisor, lease watchdogs and reconciliation."""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .processes import ProcessTree
from .protocol import Client, Interrupted, ProtocolError, Transfer, digest, key, opaque, valid_digest
from .settings import KINDS, ROOT, atomic_json, environment, inspect_tools, object_hash, profile_matches, registration, resolve_tools
from .state import State, identity, slot


ROLE_TYPES = {
    "scene_bundle": "application/zip", "review_bundle": "application/zip", "version_manifest": "application/json",
    "source_timeline": "application/json", "production_report": "application/json", "shot_metadata": "application/x-ndjson",
    "delivery_bundle": "application/zip", "white_video": "video/mp4", "comparison_video": "video/mp4",
}

OUTPUT_ROLES = {
    "prepare": {"source_timeline", "scene_bundle", "shot_metadata", "review_bundle", "version_manifest", "production_report"},
    "revise": {"scene_bundle", "shot_metadata", "review_bundle", "version_manifest", "production_report"},
    "finalize": {"white_video", "comparison_video", "delivery_bundle"},
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class LeaseWatch:
    def __init__(self, transfer, seconds, deadline, progress, events):
        self.transfer = transfer
        self.stop = transfer.stop
        self.seconds = seconds
        self.progress = progress
        self.events = events
        self.deadline = deadline
        self.closed = threading.Event()
        self.confirmed = threading.Event()
        self.directive = ""
        self.worker = threading.Thread(target=self.watch, daemon=True)

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *exc):
        self.closed.set()
        self.worker.join(timeout=12)

    def wait_for_confirmation(self):
        while not self.stop.is_set():
            if self.confirmed.wait(0.1) and not self.stop.is_set():
                return
        raise Interrupted

    def watch(self):
        while not self.closed.is_set() and not self.stop.is_set():
            before = time.monotonic()
            if before >= self.deadline:
                break
            try:
                status = self.progress()
                response, _ = self.transfer.api.json("POST", f"/work/{self.transfer.work}/heartbeat",
                                                     body=status, headers=self.transfer.headers,
                                                     timeout=max(0.2, min(7, self.deadline - before)))
                directive = response["directive"]
                if directive != "continue":
                    self.directive = directive
                    break
                now = datetime.fromisoformat(response["server_time"])
                expires = datetime.fromisoformat(response["expires_at"])
                self.deadline = before + (expires - now).total_seconds() - 2
                if self.deadline <= time.monotonic():
                    self.directive = "lease_expired"
                    break
                self.confirmed.set()
                self.events.send(status)
                self.closed.wait(max(0, min(self.seconds, self.deadline - time.monotonic())))
            except ProtocolError as exc:
                if not exc.retryable or exc.status in {401, 403, 409}:
                    self.directive = exc.code
                    break
                self.closed.wait(max(0, min(2, self.deadline - time.monotonic())))
            except (KeyError, ValueError, OSError):
                self.directive = "invalid_control"
                break
        if not self.closed.is_set():
            self.stop.set()


class Events:
    def __init__(self, state, assignment, transfer):
        self.state = state
        self.assignment = assignment
        self.transfer = transfer
        self.lock = threading.Lock()

    def send(self, status, *, level="info", message=None):
        with self.lock:
            self.state.event(self.assignment, status, level=level, message=message)
            batch = self.state.queued_events(self.assignment)
            if not batch or self.transfer.stop.is_set():
                return
            token = key(self.transfer.work, self.transfer.headers["X-Lease-Id"], batch)
            try:
                result, _ = self.transfer.api.json("POST", f"/work/{self.transfer.work}/events", body={"events":batch},
                                                  headers={**self.transfer.headers, "Idempotency-Key":token}, timeout=4)
                acknowledged = result["accepted_through"]
                if batch[0]["sequence"] <= acknowledged <= batch[-1]["sequence"]:
                    self.state.accept_events(self.assignment, acknowledged)
            except (ProtocolError, KeyError, ValueError, TypeError):
                # SQLite retains the events for the next pulse. Availability of progress logs
                # never determines whether a valid result can be committed.
                pass


class Scheduler:
    def __init__(self, config, root, token, *, test_http=False):
        self.cfg = config
        self.root = root
        self.state = State(root)
        self.api = Client(config["server"], token, test_http=test_http)
        self.tools = resolve_tools(config)
        self.versions = inspect_tools(self.tools)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=config["slots"])
        self.active = {}
        self.stop = threading.Event()
        self.drain = threading.Event()
        self.stop_after_drain = threading.Event()
        self.lock = threading.Lock()
        self.staleness = {}
        self.status = dict(connection="connecting", last_success=None)
        self.session = {}
        scope = key(config["server"], config["node_key"])
        previous = self.state.setting("node_instance")
        if previous and previous["scope"] == scope:
            self.instance_id = previous["id"]
        else:
            self.instance_id = str(uuid.uuid4())
            self.state.set("node_instance", {"scope": scope, "id": self.instance_id})

    def summary(self):
        with self.lock:
            attempts = [dict(work_id=w, attempt=n, **self.current(a["directory"], a["progress"]))
                        for (w,n), a in self.active.items()]
            state = {**self.status, "active": attempts, "draining": self.drain.is_set(),
                     "slots": self.cfg["slots"], "free_gib": shutil.disk_usage(self.root).free // 1024**3,
                     "versions": self.versions}
        history = []
        for row in self.state.rows(limit=18):
            assignment = json.loads(row["assignment"])
            status = self.state.setting("job:" + assignment["job_id"]) or {}
            history.append(dict(job_id=assignment["job_id"], work_id=row["work"], attempt=row["number"],
                                kind=assignment["kind"], status=row["status"], message=row["message"],
                                job_state=status.get("job_state"), delivery_versions=status.get("delivery_versions", [])))
        return {**state, "history": history}

    @staticmethod
    def current(directory, fallback):
        path = directory / "progress.json"
        try:
            return read(path)
        except (OSError, ValueError):
            return fallback.copy()

    def note(self, status, success=False):
        with self.lock:
            self.status["connection"] = status
            if success:
                self.status["last_success"] = datetime.now(timezone.utc).isoformat()

    def run(self):
        retry = 1
        next_poll = 0
        heartbeat_at = 0
        next_reconcile = 0
        accept = True
        try:
            while not self.stop.is_set():
                with self.lock:
                    if self.stop_after_drain.is_set() and not self.active:
                        break
                try:
                    if not self.session:
                        payload = registration(self.cfg, self.instance_id, self.versions)
                        self.work_kinds = payload["capabilities"]["work_kinds"]
                        self.session, _ = self.api.json("POST", "/workers/register", body=payload,
                                                         headers={"Idempotency-Key": key(self.instance_id, payload)})
                        self.recover()
                    now = time.monotonic()
                    with self.lock:
                        ids = [w for w, n in self.active]
                    if now >= heartbeat_at:
                        body = dict(instance_id=self.instance_id, available_slots=max(0, self.cfg["slots"]-len(ids)),
                                    active_work_ids=ids)
                        if self.drain.is_set():
                            body["available_slots"] = 0
                        answer, _ = self.api.json("POST", f"/workers/{opaque(self.session['worker_id'])}/heartbeat", body=body)
                        accept = answer["accept_claims"]
                        heartbeat_at = now + self.session["heartbeat_seconds"]
                        self.note("connected", True)
                    if now >= next_reconcile:
                        self.recover()
                        self.reconcile()
                        next_reconcile = time.monotonic() + 60
                    with self.lock:
                        free = self.cfg["slots"] - len(self.active)
                    enough = shutil.disk_usage(self.root).free >= self.cfg["disk_reserve_gib"] * 1024**3
                    if accept and enough and free > 0 and now >= next_poll and not (
                            self.drain.is_set() or self.stop_after_drain.is_set()):
                        wait = self.claim()
                        next_poll = time.monotonic() + wait
                    retry = 1
                    self.stop.wait(0.25)
                except ProtocolError as exc:
                    self.note(exc.code)
                    if exc.status in (401, 403, 404) or exc.code == "worker_unknown":
                        self.session = {}
                    self.stop.wait(max(retry, min(exc.retry_after or 0, 60)))
                    retry = min(30, retry * 2)
                except (ValueError, OSError, KeyError) as exc:
                    self.note(f"configuration: {type(exc).__name__}")
                    self.stop.wait(5)
        finally:
            with self.lock:
                for active in self.active.values():
                    active["stop"].set()
            self.pool.shutdown(wait=True, cancel_futures=True)

    def claim(self):
        pending = self.state.setting("claim")
        if not pending or (pending["body"]["instance_id"], pending["body"]["worker_id"]) != (
                self.instance_id, self.session["worker_id"]):
            # Short polls allow a node heartbeat and drain even behind a slow server.
            pending = {"key": str(uuid.uuid4()), "body": dict(worker_id=self.session["worker_id"],
                       instance_id=self.instance_id, wait_seconds=min(5, self.session["claim_wait_max_seconds"]),
                       work_kinds=self.work_kinds)}
            self.state.set("claim", pending)
        start = time.monotonic()
        assignment, wait = self.api.json("POST", "/work/claim", body=pending["body"],
                                         headers={"Idempotency-Key": pending["key"]}, timeout=10)
        if assignment:
            work, attempt = identity(assignment)
            lease = assignment["lease"]
            opaque(work)
            opaque(lease["lease_id"])
            if (lease["worker_id"] != self.session["worker_id"] or
                    type(attempt) is not int or attempt < 1 or type(lease["fence"]) is not int or lease["fence"] < 1):
                raise ValueError("服务端返回的租约身份无效")
            if self.state.start(assignment):
                self.launch(assignment, start + self.session["lease_seconds"] - 2)
        self.state.set("claim", None)
        return wait or 0

    def check_assignment(self, assignment):
        order = assignment["input_manifest"]
        expected = (assignment["job_id"], assignment["work_id"], assignment["kind"])
        if (order["job_id"], order["work_id"], order["kind"]) != expected or assignment["kind"] not in KINDS:
            raise ValueError("工单和输入清单的标识不一致")
        if object_hash(order) != assignment["input_manifest_sha256"]:
            raise ValueError("输入清单摘要不匹配")
        profile = order["profile"]
        if profile not in self.cfg["profiles"] or not profile_matches(profile, self.versions):
            raise WorkFailure({"code":"profile_mismatch", "message":"本机无法履行冻结制作档案", "retryable":False})
        roles = {artifact["role"]: artifact for artifact in order["artifacts"]}
        if (roles["source_video"]["size_bytes"] <= 0 or
                len(roles) != len(order["artifacts"]) or any(roles[role]["sha256"] != profile["compatibility"][field]
                for role, field in (("rules_bundle", "rules_sha256"), ("workflow_bundle", "workflow_sha256")))):
            raise ValueError("工单规则、流程或原片摘要不符")
        return profile, roles

    def launch(self, assignment, deadline, pending=None):
        ident = identity(assignment)
        folder = self.root / "work" / opaque(assignment["work_id"]) / str(assignment["lease"]["attempt"])
        folder.mkdir(parents=True, exist_ok=True)
        current = {"directory": folder, "stop": threading.Event(),
                   "progress": dict(stage="received", completed=0, total=None)}
        with self.lock:
            if any(work == ident[0] for work, _ in self.active) or len(self.active) >= self.cfg["slots"]:
                return False
            self.active[ident] = current
        heartbeat_seconds = self.session["lease_heartbeat_seconds"]
        self.pool.submit(self.execute, assignment, deadline, heartbeat_seconds, current, pending)
        return True

    def work_status(self, assignment):
        return self.api.json("GET", f"/jobs/{opaque(assignment['job_id'])}/status?work_id={opaque(assignment['work_id'])}")[0]

    def recover(self):
        with self.lock:
            active = set(self.active)
        free = self.cfg["slots"] - len(active)
        for row in self.state.rows(only_open=True):
            if (row["work"], row["number"]) in active:
                continue
            assignment = json.loads(row["assignment"])
            try:
                current = self.work_status(assignment)
            except ProtocolError as exc:
                if exc.status != 404:
                    raise
                self.state.mark(assignment, status="interrupted", message=exc.code)
                continue
            if current.get("receipt") and current["receipt"]["attempt"] == row["number"]:
                self.state.mark(assignment, status="succeeded", receipt=current["receipt"])
            elif current["work_state"] == "leased" and current["attempt"] == row["number"]:
                if free <= 0:
                    continue
                transfer = Transfer(self.api, assignment, self.stop, self.root / "cache/sha256")
                start = time.monotonic()
                try:
                    control, _ = self.api.json("POST", f"/work/{transfer.work}/heartbeat", headers=transfer.headers,
                                               body=dict(stage="recover", completed=0, total=None))
                except ProtocolError as exc:
                    if exc.status == 409:
                        self.state.mark(assignment, status="interrupted", message=exc.code)
                        continue
                    raise
                if control["directive"] == "continue":
                    seconds = (datetime.fromisoformat(control["expires_at"]) -
                               datetime.fromisoformat(control["server_time"])).total_seconds()
                    if self.launch(assignment, start+seconds-2, json.loads(row["pending"]) if row["pending"] else None):
                        free -= 1
                else:
                    self.state.mark(assignment, status="interrupted", message=control["directive"])
            else:
                self.state.mark(assignment, status="interrupted", message=current["work_state"])

    def reconcile(self):
        # Query one prior work per job. Old work terminal is distinct from job delivery.
        with self.lock:
            active = set(self.active)
        assignments = {}
        latest = {}
        for row in self.state.rows():
            assignment = json.loads(row["assignment"])
            job = assignment["job_id"]
            assignments.setdefault(job, []).append(assignment)
            if (row["work"], row["number"]) in active or row["status"] == "active":
                continue
            latest.setdefault(job, assignment)
        due = sorted(latest, key=lambda job: self.staleness.get(job, -3600))
        for job in due[:8]:
            if time.monotonic() - self.staleness.get(job, -3600) < 120:
                break
            self.staleness[job] = time.monotonic()
            try:
                result = self.work_status(latest[job])
            except ProtocolError as exc:
                if exc.status == 404:
                    continue
                raise
            self.state.set("job:" + job, result)
            if result["job_state"] == "deleted" and not any(identity(a) in active for a in assignments[job]):
                self.prune_deleted(job, assignments)

    def prune_deleted(self, job, assignments):
        def hashes(records):
            return {valid_digest(item["sha256"]) for a in records for item in a["input_manifest"]["artifacts"]}

        records = assignments[job]
        protected = set()
        for other_job, others in assignments.items():
            if other_job == job:
                continue
            status = self.state.setting("job:" + other_job)
            if not status or status["job_state"] != "deleted":
                protected.update(hashes(others))
        for sha in hashes(records) - protected:
            try:
                (self.root / "cache/sha256" / sha).unlink(missing_ok=True)
            except PermissionError:
                # A Windows file may still be open. A later reconciliation retries cleanup.
                pass
        for work in {opaque(a["work_id"]) for a in records}:
            shutil.rmtree(self.root / "work" / work, ignore_errors=True)

    def execute(self, assignment, deadline, heartbeat_seconds, current, pending):
        try:
            self.execute_attempt(assignment, deadline, heartbeat_seconds, current, pending)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            self.state.mark(assignment, message=f"执行工单初始化或收尾失败 · {type(exc).__name__}")
        finally:
            with self.lock:
                del self.active[identity(assignment)]

    def execute_attempt(self, assignment, deadline, heartbeat_seconds, current, pending):
        directory = current["directory"]
        transfer = Transfer(self.api, assignment, current["stop"], self.root / "cache/sha256")
        progress = lambda: self.current(directory, current["progress"])
        events = Events(self.state, assignment, transfer)
        watch = LeaseWatch(transfer, heartbeat_seconds, deadline, progress, events)
        try:
            with watch:
                watch.wait_for_confirmation()
                try:
                    def report(stage):
                        atomic_json(directory / "progress.json", dict(stage=stage, completed=0, total=None))
                    if pending is None:
                        profile, artifacts = self.check_assignment(assignment)
                        checkpoint = self.load_checkpoint(assignment, directory, transfer.stop)
                        if checkpoint is None:
                            if (directory.parent / "outputs.json").exists():
                                # An existing checkpoint failed verification. Ask the media
                                # runner to recreate the output set from its validated inputs.
                                (directory / "result.json").unlink(missing_ok=True)
                            report("download")
                            with slot(self.root / "resources", "transfer", self.cfg["transfer_slots"], transfer.stop):
                                paths = {role: str(transfer.download(artifact, directory)) for role, artifact in artifacts.items()}
                            if transfer.stop.is_set():
                                raise Interrupted
                            pending = self.produce(assignment, profile, paths, directory, transfer, progress)
                        else:
                            report("reuse_outputs")
                            pending = self.upload_result(assignment, profile, checkpoint, directory, transfer)
                    events.send(dict(stage="commit_result", completed=0, total=None))
                    receipt = transfer.json("POST", f"/work/{transfer.work}/complete", pending, "complete")
                    self.state.mark(assignment, status="succeeded", receipt=receipt)
                except (ValueError, KeyError, OSError, RuntimeError) as exc:
                    if not isinstance(exc, WorkFailure):
                        error = {"code": "input_invalid" if isinstance(exc, (ValueError, KeyError)) else "other",
                                 "message": f"{progress()['stage']} · {type(exc).__name__}", "retryable": isinstance(exc, OSError)}
                    else:
                        error = exc.body
                    events.send(dict(stage="report_failure", completed=0, total=None),
                                level="error", message=error["message"])
                    transfer.json("POST", f"/work/{transfer.work}/fail", error, "fail")
                    self.state.mark(assignment, status="failed", message=error["message"])
        except (Interrupted, ProtocolError) as exc:
            try:
                result = self.work_status(assignment)
                receipt = result.get("receipt")
                if receipt and receipt["attempt"] == assignment["lease"]["attempt"]:
                    self.state.mark(assignment, status="succeeded", receipt=receipt)
                elif result["work_state"] != "leased" or result["attempt"] != assignment["lease"]["attempt"]:
                    self.state.mark(assignment, status="interrupted", message=watch.directive or result["work_state"])
                else:
                    self.state.mark(assignment, message=watch.directive or str(exc))
            except (ProtocolError, KeyError, ValueError):
                self.state.mark(assignment, message=watch.directive or str(exc))

    def produce(self, assignment, profile, paths, directory, transfer, progress):
        result_file = directory / "result.json"
        if not result_file.is_file():
            request = dict(assignment=assignment, paths=paths, tools=self.tools,
                           environment=environment(self.cfg, self.versions, profile),
                           resources=str(self.root / "resources"), cpu_threads=self.cfg["cpu_threads"],
                           model_slots=self.cfg["model_slots"], render_slots=self.cfg["render_slots"])
            atomic_json(directory / "request.json", request)
            env = {name: value for name, value in os.environ.items() if name != "FILM_WORKER_TOKEN"}
            env["PYTHONUTF8"] = "1"
            with (directory / "worker.log").open("ab") as log:
                tree = ProcessTree([sys.executable, "-m", "film_worker", "child", str(directory)],
                                   cwd=ROOT, env=env, log=log)
                try:
                    while tree.child.poll() is None:
                        if transfer.stop.wait(0.25):
                            raise Interrupted
                finally:
                    tree.close()
            if tree.child.returncode or not result_file.is_file():
                error_file = directory / "error.json"
                error = read(error_file) if error_file.is_file() else dict(code="render_failed", message="制作进程中止")
                raise WorkFailure(dict(error, retryable=error["code"] not in {"input_invalid", "profile_mismatch"}))
        result = read(result_file)
        if set(result["outputs"]) != OUTPUT_ROLES[assignment["kind"]]:
            raise ValueError("产物角色与工单类型不符")
        artifacts = {}
        for role, raw in result["outputs"].items():
            path = Path(raw).resolve()
            if not path.is_relative_to(directory.resolve()) or not path.is_file():
                raise ValueError("产物路径越界或文件不存在")
            if transfer.stop.is_set():
                raise Interrupted
            artifacts[role] = {"name": str(path.relative_to(directory.resolve())), "sha256": digest(path),
                               "size": path.stat().st_size}
        checkpoint = dict(result, outputs=artifacts, attempt=assignment["lease"]["attempt"],
                          input_manifest_sha256=assignment["input_manifest_sha256"], versions=self.versions)
        atomic_json(directory.parent / "outputs.json", checkpoint)
        return self.upload_result(assignment, profile, checkpoint, directory, transfer)

    def load_checkpoint(self, assignment, directory, stopped):
        try:
            checkpoint = read(directory.parent / "outputs.json")
            attempt = checkpoint["attempt"]
            if (type(attempt) is not int or not 1 <= attempt <= assignment["lease"]["attempt"] or
                    checkpoint["input_manifest_sha256"] != assignment["input_manifest_sha256"] or
                    checkpoint["versions"] != self.versions or
                    set(checkpoint["outputs"]) != OUTPUT_ROLES[assignment["kind"]]):
                return None
            for artifact in checkpoint["outputs"].values():
                if stopped.is_set():
                    raise Interrupted
                path = self.checkpoint_path(directory, attempt, artifact)
                if digest(path) != artifact["sha256"]:
                    return None
            return checkpoint
        except (KeyError, OSError, ValueError, TypeError):
            return None

    @staticmethod
    def checkpoint_path(directory, attempt, artifact):
        version = (directory.parent / str(attempt)).resolve()
        path = (version / artifact["name"]).resolve()
        if not path.is_relative_to(version) or path.stat().st_size != artifact["size"]:
            raise ValueError("产物路径或长度与检查点不符")
        return path

    def upload_result(self, assignment, profile, result, directory, transfer):
        outputs = []
        with slot(self.root / "resources", "transfer", self.cfg["transfer_slots"], transfer.stop):
            for role, artifact in result["outputs"].items():
                path = self.checkpoint_path(directory, result["attempt"], artifact)
                def update(done, total):
                    atomic_json(directory / "progress.json", dict(stage="upload_" + role, completed=done, total=total))
                outputs.append(transfer.upload(role, path, ROLE_TYPES[role], update, expected_sha=artifact["sha256"]))
        payload = dict(input_manifest_sha256=assignment["input_manifest_sha256"], profile_hash=profile["profile_hash"],
                       environment=result["environment"], outputs=outputs)
        if "final_verification" in result:
            payload["final_verification"] = result["final_verification"]
        self.state.mark(assignment, pending=payload)
        return payload


class WorkFailure(RuntimeError):
    def __init__(self, body):
        super().__init__(body["message"])
        self.body = body
