"""Tests for the distributed worker's new protocol and content boundaries."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from film_worker import bundles, production, quality
from film_worker.processes import ProcessTree
from film_worker.protocol import Client, Transfer
from film_worker.settings import DEFAULTS, object_hash, registration
from film_worker.state import State


def sha(data):
    return hashlib.sha256(data).hexdigest()


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), MockHandler)
        self.blob = b"movie-worker-artifact"
        self.begin = None
        self.uploaded = {}
        self.received_ranges = []

    def upload(self):
        parts = [dict(part_no=i, start=start, end=start+len(data)-1, sha256=sha(data))
                 for i, (start, data) in sorted(self.uploaded.items())]
        size = self.begin["size_bytes"]
        return dict(self.begin, upload_id="u1", work_id="w1", state="open", part_size_bytes=8388608,
                    part_count=(size+8388607)//8388608, received_parts=parts)


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, status, body=None, **headers):
        self.send_response(status)
        if body is not None:
            body = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_header("Content-Length", str(len(body)))
        for header, value in headers.items():
            self.send_header(header, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        server = self.server
        if self.path.startswith("/api/v1/artifacts/"):
            self.send_artifact(server)
        elif self.path == "/api/v1/uploads/u1":
            self.answer(200, server.upload())
        else:
            self.answer(404)

    def send_artifact(self, server):
        data = server.blob
        etag = f'"{sha(data)}"'
        if self.headers.get("Range"):
            start = int(self.headers["Range"].split("=")[1][:-1])
            server.received_ranges.append((start, self.headers.get("If-Range")))
            self.answer(206, data[start:], ETag=etag,
                        **{"Content-Range": f"bytes {start}-{len(data)-1}/{len(data)}"})
        else:
            self.answer(200, data, ETag=etag)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server = self.server
        if self.path.endswith("/work/w1/uploads"):
            server.begin = body
            self.answer(201, server.upload())
        elif self.path.endswith("/uploads/u1/complete"):
            checked = b"".join(server.uploaded[i][1] for i in range(1, body["part_count"]+1))
            if sha(checked) != body["sha256"]:
                return self.answer(422, {"code": "hash_mismatch"})
            self.answer(200, dict(server.begin, artifact_id="written"))
        else:
            self.answer(404)

    def do_PUT(self):
        if self.headers.get("X-Fence") != "2" or self.headers.get("X-Lease-Id") != "lease":
            return self.answer(409, {"code": "lease_lost"})
        part = int(self.path.rsplit("/", 1)[1])
        data = self.rfile.read(int(self.headers["Content-Length"]))
        if sha(data) != self.headers["X-Part-SHA256"]:
            return self.answer(422, {"code": "hash_mismatch"})
        start = int(self.headers["Content-Range"].split()[1].split("-")[0])
        self.server.uploaded[part] = (start, data)
        self.answer(204)


class TransferTests(unittest.TestCase):
    def test_range_resume_and_exact_multipart_wire_format(self):
        with tempfile.TemporaryDirectory() as tmp, MockServer() as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                host = f"http://127.0.0.1:{server.server_port}"
                api = Client(host, "test", test_http=True)
                assignment = dict(work_id="w1", lease=dict(lease_id="lease", attempt=1, fence=2))
                root = Path(tmp)
                part = root / (sha(server.blob)+".part")
                part.write_bytes(server.blob[:7])
                transfer = Transfer(api, assignment, threading.Event(), root/"cache")
                path = transfer.download(dict(artifact_id="a1", sha256=sha(server.blob),
                                              size_bytes=len(server.blob)), root)
                self.assertEqual(path.read_bytes(), server.blob)
                self.assertEqual(server.received_ranges, [(7, f'"{sha(server.blob)}"')])
                payload = root/"payload.zip"
                payload.write_bytes(b"x"*8388608 + b"last part")
                actual = transfer.upload("review_bundle", payload, "application/zip")
                self.assertEqual(actual["artifact_id"], "written")
                self.assertEqual(actual["sha256"], sha(payload.read_bytes()))
                self.assertEqual(server.uploaded[2], (8388608, b"last part"))
            finally:
                server.shutdown()
                thread.join()


class ProcessTests(unittest.TestCase):
    def test_closing_tree_kills_grandchild_after_leader_exit(self):
        with tempfile.TemporaryDirectory(prefix="worker-process-") as tmp:
            root = Path(tmp)
            ready, survived = root/"ready", root/"survived"
            subprocess_code = """import os, signal, sys, time
from pathlib import Path
if os.name != "nt":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).touch()
time.sleep(2)
Path(sys.argv[2]).touch()
"""
            leader_code = """import os, subprocess, sys
if os.read(sys.stdin.fileno(),1)==b"1":
    subprocess.Popen(sys.argv[1:])
"""
            args = [sys.executable,"-c",leader_code,sys.executable,"-c",subprocess_code,str(ready),str(survived)]
            with (root/"worker.log").open("wb") as log:
                tree = ProcessTree(args,cwd=root,env=os.environ,log=log)
                try:
                    tree.child.wait(timeout=5)
                    deadline = time.monotonic()+5
                    while not ready.exists() and time.monotonic()<deadline:
                        time.sleep(0.05)
                    self.assertTrue(ready.exists())
                finally:
                    tree.close()
                time.sleep(2.1)
                self.assertFalse(survived.exists())


class ArtifactTests(unittest.TestCase):
    def test_bundle_rejects_path_traversal_and_windows_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp = Path(tmp)
            for i, member in enumerate(["../secret", "nested/CON.txt", "nested\\danger"]):
                archive = temp / f"invalid{i}.zip"
                with zipfile.ZipFile(archive, "w") as z:
                    # ZipFile normalizes path separators when given a filename on Windows.
                    raw = zipfile.ZipInfo("placeholder")
                    raw.filename = member
                    z.writestr(raw, b"bad")
                with self.subTest(member=member):
                    with self.assertRaises(ValueError):
                        bundles.extract(archive, temp/f"unpacked{i}", indexed=False)

    def test_bundle_roundtrip_and_idempotent_completion_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contents = root/"contents"
            contents.mkdir()
            (contents/"scene.blend").write_bytes(b"scene")
            bundles.build(contents, {"schema_version": 1}, root/"asset.zip")
            index = bundles.extract(root/"asset.zip", root/"unpacked", indexed=True)
            self.assertEqual(index["files"][0]["sha256"], sha(b"scene"))
            state = State(root)
            assignment = {"work_id": "same", "lease": {"attempt": 1}}
            state.start(assignment)
            state.mark(assignment, pending={"outputs": ["fixture"]})
            state.start(assignment)
            self.assertEqual(len(state.rows(True)), 1)
            self.assertEqual(json.loads(state.rows(True)[0]["pending"]), {"outputs": ["fixture"]})

    def test_incompatible_media_tool_build_names_the_exact_difference(self):
        profile = {"compatibility":{"ffmpeg":"9.0.2"}, "codex_model":"fixture",
                   "codex_provider":"fixture", "reasoning_effort":"medium"}
        profile["profile_hash"] = object_hash(profile)
        with self.assertRaisesRegex(ValueError,"ffmpeg 要求 9.0.2，本机 8.1"):
            registration({**DEFAULTS,"profiles":[profile]}, "fixture", {"ffmpeg":"8.1"})

    def test_formal_batch_prohibits_automatic_authoring(self):
        profile = {"production_policy": "human_required"}
        self.assertFalse(quality.may_author(profile, "prepare"))
        self.assertFalse(quality.may_author(profile, "revise"))
        self.assertTrue(quality.may_author(profile, "finalize"))
        self.assertTrue(quality.may_author({}, "prepare"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg and FFprobe")
    def test_black_last_frame_is_reported_with_one_based_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"clip.mp4"
            subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","color=white:size=128x72:rate=24",
                            "-frames:v","3","-vf","drawbox=t=fill:c=black:enable='eq(n,2)'",
                            "-c:v","mpeg4",str(path)], check=True)
            data = quality.read_video("white_video", path, scan_black=True)
            self.assertEqual(data["black_frames"], [3])
            self.assertEqual(data["black_tail_frames"], [3])

    def test_odd_review_dimensions_and_variable_tick_conversion(self):
        timeline = {"pts": [0,20,70], "end": 130, "time_base": "1/1000", "fps": "300/13",
                    "source_sha256": "0"*64, "variable_rate": True, "audio_offset": "0",
                    "audio_index": None, "source_size": [720,404], "display_size": [720,404],
                    "width": 3840, "height": 2154}
        profile = {"review_landscape_limit": dict(width=1920, height=1080)}
        self.assertEqual(production.review_dimensions(timeline, profile), (1920, 1077))
        wire = production.to_wire(timeline, {"decoder_id": "fixture"})
        self.assertEqual([row["duration_pts"] for row in wire["frames"]], [20,50,60])
        self.assertEqual(wire["end_pts"], 130)


if __name__ == "__main__":
    unittest.main()
