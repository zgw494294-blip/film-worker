"""Run the worker scheduler against an HTTP service with an end-to-end lease lifecycle."""
import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from film_worker.scheduler import Scheduler
from film_worker.settings import DEFAULTS, atomic_json, environment, object_hash


ROLES = {
    "prepare": ["source_timeline", "scene_bundle", "shot_metadata", "review_bundle", "version_manifest", "production_report"],
    "revise": ["scene_bundle", "shot_metadata", "review_bundle", "version_manifest", "production_report"],
    "finalize": ["white_video", "comparison_video", "delivery_bundle"],
}
VERSIONS = {"blender": "5.2.3", "ffmpeg": "9.0.2", "codex_cli": "0.169.1", "decoder_id": "fixture"}


def now(offset=0):
    return (datetime.now(timezone.utc)+timedelta(seconds=offset)).isoformat()


def sha(content):
    return hashlib.sha256(content).hexdigest()


class ProductionMock(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Fixture)
        self.mutex = threading.Lock()
        self.input = b"fixture-input"
        self.profile = dict(compatibility=dict(VERSIONS, runner_commit="a"*40, workflow_sha256=sha(self.input),
                                                rules_sha256=sha(self.input)), final_width=3840, color_policy="sRGB",
                            signature_scheme="pair-rgb8-timing-v1", codex_model="fixture", codex_provider="fixture",
                            reasoning_effort="medium", review_landscape_limit={"width":1920,"height":1080},
                            review_portrait_limit={"width":1080,"height":1920})
        self.profile["profile_hash"] = object_hash(self.profile)
        self.registered = []
        self.queue = []
        self.attempts = {}
        self.claims = {}
        self.uploads = {}
        self.replayed = {}
        self.events = {}
        self.completed = []
        self.release_drop = threading.Event()

    def schedule(self, kind, version):
        work = f"work-{kind}"
        artifacts = [dict(artifact_id="asset", role=role, sha256=sha(self.input), size_bytes=len(self.input),
                          media_type="application/octet-stream")
                     for role in ("source_video", "rules_bundle", "workflow_bundle")]
        order = dict(schema_version=1, job_id="job1", work_id=work, kind=kind,
                     base_version=None if kind=="prepare" else (version-1 if kind=="revise" else version),
                     target_version=version, profile=self.profile, artifacts=artifacts)
        assignment = dict(job_id="job1", work_id=work, kind=kind, input_manifest=order,
                          input_manifest_sha256=object_hash(order),
                          lease=dict(lease_id="lease-"+kind, fence=7, attempt=1, worker_id="node", expires_at=now(85)))
        with self.mutex:
            self.queue.append(assignment)
            self.attempts[work] = {"assignment":assignment, "state":"queued"}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def answer(self, status, content=None, **headers):
        self.send_response(status)
        if content is not None:
            content = content if isinstance(content, bytes) else json.dumps(content).encode()
            self.send_header("Content-Length", str(len(content)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if content:
            self.wfile.write(content)

    def do_GET(self):
        self.handle_fixture("GET")

    def do_POST(self):
        self.handle_fixture("POST")

    def do_PUT(self):
        self.handle_fixture("PUT")

    def handle_fixture(self, method):
        if self.headers.get("Authorization") != "Bearer fixture":
            return self.answer(401, {"code":"unauthenticated"})
        length = int(self.headers.get("Content-Length",0))
        raw = self.rfile.read(length) if length else b""
        payload = json.loads(raw) if method=="POST" and raw else {}
        path = self.path.removeprefix("/api/v1/")
        token = self.headers.get("Idempotency-Key")
        server = self.server
        with server.mutex:
            if path=="workers/register":
                server.registered.append(payload)
                return self.answer(200, dict(worker_id="node", server_time=now(), heartbeat_seconds=30,
                                            claim_wait_max_seconds=25, lease_seconds=90, lease_heartbeat_seconds=20,
                                            part_size_bytes=8388608))
            if path=="workers/node/heartbeat":
                return self.answer(200, dict(server_time=now(), accept_claims=True))
            if path=="work/claim":
                if token not in server.claims:
                    server.claims[token] = server.queue.pop(0) if server.queue else None
                assignment = server.claims[token]
                if assignment:
                    server.attempts[assignment["work_id"]]["state"] = "leased"
                    return self.answer(200, assignment)
                return self.answer(204, **{"Retry-After":"1"})
            if path.startswith("jobs/job1/status?work_id="):
                work = path.split("=")[-1]
                attempt = server.attempts[work]
                state = attempt["state"]
                result = dict(job_id="job1",work_id=work,job_state="done" if "work-finalize" in server.completed else "review",
                              work_state=state,attempt=1,directive="continue",current_version=len(server.completed),
                              delivery_versions=[2] if "work-finalize" in server.completed else [],
                              terminal=state=="succeeded",server_time=now())
                if "receipt" in attempt:
                    result["receipt"] = attempt["receipt"]
                return self.answer(200, result)
            if path.startswith("artifacts/"):
                return self.answer(200, server.input, ETag=f'"{sha(server.input)}"')
            if path.startswith("uploads/"):
                upload_id = path.split("/")[1]
                upload = server.uploads[upload_id]
                if path.endswith("/parts/1"):
                    if self.headers.get("X-Part-SHA256") != sha(raw):
                        return self.answer(422, {"code":"hash_mismatch"})
                    upload["received_parts"] = [dict(part_no=1,start=0,end=len(raw)-1,sha256=sha(raw))]
                    return self.answer(204)
                if path.endswith("/complete"):
                    artifact = {k:upload[k] for k in ("role","sha256","size_bytes","media_type")}
                    upload["state"] = "complete"
                    upload["artifact"] = dict(artifact,artifact_id=upload_id)
                    return self.answer(200,upload["artifact"])
                return self.answer(200,upload)
            if path.startswith("work/"):
                return self.work(server, path, payload, token)
            return self.answer(404,{"code":"missing"})

    def work(self, server, path, payload, token):
        work, command = path.split("/")[1:3]
        attempt = server.attempts[work]
        kind = attempt["assignment"]["kind"]
        if self.headers.get("X-Lease-Id") != "lease-"+kind or self.headers.get("X-Fence") != "7":
            return self.answer(409,{"code":"lease_lost"})
        if command=="heartbeat":
            return self.answer(200,dict(directive="continue",server_time=now(),expires_at=now(85)))
        if command=="events":
            history = server.events.setdefault(work,{})
            for event in payload["events"]:
                seq = event["sequence"]
                if seq in history and history[seq] != event:
                    return self.answer(409, {"code":"event_conflict"})
                if seq not in history and seq != len(history)+1:
                    return self.answer(409, {"code":"event_sequence"})
                history[seq] = event
            return self.answer(200, {"accepted_through":len(history)})
        if command=="uploads":
            upload_id = f"up-{kind}-{payload['role']}"
            if upload_id not in server.uploads:
                server.uploads[upload_id] = dict(payload,upload_id=upload_id,work_id=work,state="open",
                                                  part_count=1,part_size_bytes=8388608,received_parts=[])
            return self.answer(201,server.uploads[upload_id])
        if command=="complete":
            if {r["role"] for r in payload["outputs"]} != set(ROLES[kind]):
                return self.answer(422,{"code":"invalid_manifest"})
            if "receipt" in attempt:
                if attempt["token"] != token or attempt["body"] != payload:
                    return self.answer(409,{"code":"idempotency_conflict"})
                server.replayed[work] = True
                return self.answer(200,attempt["receipt"])
            version = attempt["assignment"]["input_manifest"]["target_version"]
            receipt = dict(receipt_id="receipt-"+kind,kind=kind,state="succeeded",version=version,
                           job_id="job1",work_id=work,attempt=1,committed_at=now(),artifacts=payload["outputs"])
            attempt.update(receipt=receipt,body=payload,token=token,state="succeeded")
            server.completed.append(work)
            if kind=="prepare":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            return self.answer(200,receipt)
        return self.answer(404,{"code":"missing"})


class WorkerFlow(unittest.TestCase):
    def test_registration_to_review_and_final_delivery_with_lost_receipt(self):
        with tempfile.TemporaryDirectory(prefix="worker-flow-") as folder, ProductionMock() as service:
            root = Path(folder)
            service_thread=threading.Thread(target=service.serve_forever)
            service_thread.start()
            def fixture_outputs(self, assignment, profile, paths, directory, transfer, progress):
                env = environment(self.cfg,self.versions,profile)
                outputs={}
                for role in ROLES[assignment["kind"]]:
                    path = directory/(role+".bin")
                    path.write_bytes((role+assignment["kind"]).encode())
                    outputs[role]=str(path)
                atomic_json(directory/"result.json",dict(outputs=outputs,environment=env))
                return original(self, assignment, profile, paths, directory, transfer, progress)

            config={**DEFAULTS,"node_key":"fixture","server":f"http://127.0.0.1:{service.server_port}",
                    "profiles":[service.profile],"disk_reserve_gib":1}
            original = Scheduler.produce
            service.schedule("prepare",1)
            with patch("film_worker.scheduler.resolve_tools",return_value={}), patch(
                    "film_worker.scheduler.inspect_tools",return_value=VERSIONS), patch.object(Scheduler,"produce",fixture_outputs):
                node=Scheduler(config,root,"fixture",test_http=True)
                worker=threading.Thread(target=node.run)
                worker.start()
                try:
                    self.wait_for(lambda: service.replayed.get("work-prepare"), node)
                    self.wait_for(lambda: node.state.rows()[0]["status"]=="succeeded", node)
                    self.assertEqual(len(service.registered),1)
                    self.assertEqual(service.completed,["work-prepare"])
                    # Model the human submit/pass events by making the next server-side work visible.
                    service.schedule("revise",2)
                    self.wait_for(lambda:"work-revise" in service.completed,node)
                    service.schedule("finalize",2)
                    self.wait_for(lambda:len(node.state.rows())==3 and all(
                        r["status"]=="succeeded" for r in node.state.rows()),node)
                    node.reconcile()
                    self.assertEqual(node.state.setting("job:job1")["delivery_versions"],[2])
                    self.assertEqual(service.completed,["work-prepare","work-revise","work-finalize"])
                    self.assertIn("commit_result", {e["stage"] for e in service.events["work-prepare"].values()})
                finally:
                    node.stop.set()
                    worker.join(timeout=20)
                    service.shutdown()
                    service_thread.join(timeout=10)

    def wait_for(self, condition, node):
        limit=time.monotonic()+12
        while time.monotonic()<limit:
            if condition():
                return
            time.sleep(0.05)
        self.fail(str(node.summary()))
