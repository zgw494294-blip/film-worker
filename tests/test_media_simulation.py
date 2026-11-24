"""Exercise real artifact production and return transfer against the local contract fixture."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from film_worker.processes import ProcessTree
from film_worker.scheduler import Scheduler
from film_worker.settings import ROOT, inspect_tools, object_hash, resolve_tools
from test_server_flow import mock_service, node_config, sha


REFERENCES = ("version_manifest","scene_bundle","shot_metadata","review_bundle","production_report")


def archive(label):
    data = io.BytesIO()
    with zipfile.ZipFile(data,"w") as z:
        z.writestr("fixture.txt",label)
    return data.getvalue()


def json_blob(data):
    return json.dumps(data,ensure_ascii=False).encode()


def start_fixture(command, *, cwd, env, log):
    worker = Path(__file__).with_name("media_child.py")
    return ProcessTree([command[0],str(worker),command[-1]],cwd=cwd,env={**env,"PYTHONPATH":str(ROOT)},log=log)


@unittest.skipUnless(os.environ.get("FILM_WORKER_TEST_SIMULATION")=="1","requires local Blender and media tools")
class WholeMediaSimulation(unittest.TestCase):
    def wait_work(self, service, worker, work):
        end = time.monotonic()+70
        while time.monotonic()<end:
            with service.mutex:
                job = service.attempts[work]
                if job["state"]=="succeeded" and not worker.summary()["active"]:
                    return
                if job["state"]=="failed":
                    log = worker.root/"work"/work/"1/worker.log"
                    self.fail(f"{job['error']}\n{log.read_text(encoding='utf-8',errors='replace')[-1800:] if log.exists() else ''}")
            time.sleep(0.15)
        self.fail(str(worker.summary()))

    def test_http_roundtrip_with_real_blender_1080p_4k_and_audio(self):
        with tempfile.TemporaryDirectory(prefix="worker-local-manager-") as directory, mock_service() as service:
            root = Path(directory)
            data = root/"节点 数据"
            data.mkdir()
            config = node_config(service)
            config["render_backend"] = "cpu"
            config["cpu_threads"] = 2
            for name in ("blender","codex","ffmpeg","ffprobe"):
                if binary := os.environ.get(f"FILM_TEST_{name.upper()}"):
                    config[name] = binary
            tools = resolve_tools(config)
            versions = inspect_tools(tools)
            clip = root/"input.mp4"
            subprocess.run([tools["ffmpeg"],"-v","error","-f","lavfi","-i","testsrc2=size=640x360:rate=24",
                            "-f","lavfi","-i","sine=frequency=440:sample_rate=44100","-t","0.125",
                            "-c:v","mpeg4","-c:a","aac",str(clip)],check=True)
            source = service.store("source_video",clip.read_bytes(),"video/mp4")
            rules = service.store("rules_bundle",archive("test rules"),"application/zip")
            workflow = service.store("workflow_bundle",archive("test workflow"),"application/zip")
            defaults = {"source_video":source,"rules_bundle":rules,"workflow_bundle":workflow}
            profile = service.profile
            profile["compatibility"].update(versions,rules_sha256=rules["sha256"],workflow_sha256=workflow["sha256"])
            profile.pop("profile_hash")
            profile["profile_hash"] = object_hash(profile)

            with patch("film_worker.scheduler.ProcessTree",start_fixture):
                node = Scheduler(config,data,"fixture",test_http=True)
                worker = threading.Thread(target=node.run)
                try:
                    first = service.schedule("prepare",1,artifacts=list(defaults.values()))
                    worker.start()
                    self.wait_work(service,node,first)
                    self.assertEqual(service.completed,[first],"人工审核前应等待")
                    artifacts = service.blobs_from(first)
                    version1 = json.loads(artifacts["version_manifest"])
                    frame1 = version1["frames"][1]
                    timeline = service.outputs_from(first,"source_timeline")
                    self.assertEqual(version1["review_size"],{"width":1920,"height":1080})
                    self.assertEqual(len(version1["frames"]),3)
                    with zipfile.ZipFile(io.BytesIO(artifacts["review_bundle"])) as reviews:
                        self.assertIn(frame1["pair_jpeg_path"],reviews.namelist())

                    status = dict(schema_version=1,job_id="job1",base_version=1,original_annotator_id="alice",
                                  base_manifest_sha256=sha(artifacts["version_manifest"]),
                                  timeline_sha256=version1["timeline_sha256"])
                    feedback = dict(status,origin="review",review_size=version1["review_size"],
                                    frames=[dict(number=2,review_signature=frame1["review_signature"],
                                                note="主体右移",regions=[])])
                    feedback_artifact = service.store("feedback_manifest",json_blob(feedback),"application/json")
                    approve = self.approval(service,version1,status,all_frames=False)
                    revised = service.schedule("revise",2,artifacts=[*defaults.values(),*timeline,
                        *service.outputs_from(first,*REFERENCES),feedback_artifact,approve])
                    self.wait_work(service,node,revised)
                    new = service.blobs_from(revised)
                    version2 = json.loads(new["version_manifest"])
                    self.assertNotEqual(version2["frames"][1]["review_signature"],frame1["review_signature"])
                    for i in (0,2):
                        self.assertEqual(version1["frames"][i]["review_signature"],version2["frames"][i]["review_signature"])
                    self.assertEqual(version1["source_sha256"],version2["source_sha256"])
                    self.assertFalse(any(w=="work-finalize" for w in service.completed),"全部通过前不得出片")
                    status.update(base_version=2,base_manifest_sha256=sha(new["version_manifest"]))
                    approve = self.approval(service,version2,status,all_frames=True)
                    export = service.schedule("finalize",2,artifacts=[*defaults.values(),*timeline,
                        *service.outputs_from(revised,*REFERENCES),approve],drop_reply=True)
                    self.wait_work(service,node,export)
                    result = service.blobs_from(export)
                    receipt = service.attempts[export]["body"]["final_verification"]
                    self.assertEqual(receipt["white_size"],{"width":3840,"height":2160})
                    self.assertEqual(receipt["comparison_size"],{"width":3840,"height":4320})
                    self.assertEqual(receipt["verified_review_frames"],3)
                    self.assertTrue(service.replayed[export])
                    with zipfile.ZipFile(io.BytesIO(result["delivery_bundle"])) as deliver:
                        self.assertEqual(deliver.read("target/source.mp4"),clip.read_bytes())
                        params=[json.loads(row) for row in deliver.read("meta/video-technical.jsonl").splitlines()]
                        self.assertEqual([i["role"] for i in params],["source_video","white_video","comparison_video"])
                        self.assertEqual(params[1]["audio"][0]["sample_rate"],48000)
                    self.assertEqual(service.completed,[first,revised,export])
                finally:
                    node.stop.set()
                    if worker.ident:
                        worker.join(timeout=20)

    def approval(self, service, version, status, *, all_frames):
        subset = version["frames"] if all_frames else [version["frames"][0]]
        entry = dict(status,scope="all_frames" if all_frames else "approved_subset",
                     **{k:version[k] for k in ("source_sha256","profile_hash","scene_sha256","frame_count")})
        entry["frames"] = [dict(number=f["number"],review_signature=f["review_signature"],
                                approved_version=1 if f["number"]==1 else version["version"]) for f in subset]
        return service.store("approval_manifest",json_blob(entry),"application/json")
