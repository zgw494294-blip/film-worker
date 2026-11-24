"""Exercise real artifact production and return transfer against the local contract fixture."""
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from film_worker import intake
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


def source_clip(ffmpeg, path, *, variable):
    duration = "0.16667" if variable else "0.125"
    select = ["-vf", r"select=not(eq(n\,1))"] if variable else []
    subprocess.run([ffmpeg,"-v","error","-f","lavfi","-i",f"testsrc2=size=640x360:rate=24:duration={duration}",
                    "-f","lavfi","-i",f"sine=frequency=440:sample_rate=44100:duration={duration}",
                    *select,"-fps_mode","vfr","-frames:v","3","-c:v","mpeg4","-c:a","aac",str(path)],check=True)


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
        self.roundtrip(variable=False)

    def test_variable_source_timing_survives_revision_by_another_node_and_4k(self):
        self.roundtrip(variable=True)

    def roundtrip(self, *, variable):
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
            source_clip(tools["ffmpeg"],clip,variable=variable)
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
                    clock = json.loads(artifacts["source_timeline"])
                    self.assertIs(clock["variable_rate"],variable)
                    base = Fraction(clock["time_base"])
                    durations = [Fraction(n,24) for n in ((2,1,1) if variable else (1,1,1))]
                    self.assertEqual([f["duration_pts"]*base for f in clock["frames"]],durations)
                    self.assertEqual(version1["review_size"],{"width":1920,"height":1080})
                    self.assertEqual(len(version1["frames"]),3)
                    with zipfile.ZipFile(io.BytesIO(artifacts["review_bundle"])) as reviews:
                        self.assertIn(frame1["pair_jpeg_path"],reviews.namelist())

                    status = dict(schema_version=1,job_id="job1",base_version=1,original_annotator_id="alice",
                                  base_manifest_sha256=sha(artifacts["version_manifest"]),
                                  timeline_sha256=version1["timeline_sha256"])
                    feedback = dict(status,origin="review",review_size=version1["review_size"],
                                    frames=[dict(number=2,review_signature=frame1["review_signature"],
                                                note="主体右移",regions=[dict(x=0.3,y=0.64,w=0.4,h=0.3,note="核对主体位置")])])
                    feedback_artifact = service.store("feedback_manifest",json_blob(feedback),"application/json")
                    approve = self.approval(service,version1,status,all_frames=False)
                    if variable:
                        node.stop.set()
                        worker.join(timeout=20)
                        self.assertFalse(worker.is_alive())
                        other = root/"另一台节点"
                        other.mkdir()
                        node = Scheduler({**config,"node_key":"fixture-handoff"},other,"fixture",test_http=True)
                        worker = threading.Thread(target=node.run)
                        worker.start()
                    revised = service.schedule("revise",2,artifacts=[*defaults.values(),*timeline,
                        *service.outputs_from(first,*REFERENCES),feedback_artifact,approve])
                    self.wait_work(service,node,revised)
                    self.assertEqual(service.workers["fixture"]!=node.session["worker_id"],variable)
                    self.assertTrue((node.root/"work"/revised/"1/scene/feedback-images/0002.jpg").is_file())
                    new = service.blobs_from(revised)
                    version2 = json.loads(new["version_manifest"])
                    changes = [before["number"] for before,after in zip(version1["frames"],version2["frames"],strict=True)
                               if before["review_signature"]!=after["review_signature"]]
                    self.assertEqual(changes,[2,3] if variable else [2])
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
                    for role in ("white_video","comparison_video"):
                        output = root/(role+".mp4")
                        output.write_bytes(result[role])
                        with patch.dict(os.environ,{"FILM_WORKER_FFPROBE":tools["ffprobe"]}):
                            _,_,pts,timescale,duration,_ = intake.measure(output)
                        self.assertEqual([n*timescale for n in pts],[f["pts"]*base for f in clock["frames"]])
                        self.assertEqual(duration,sum(durations))
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
