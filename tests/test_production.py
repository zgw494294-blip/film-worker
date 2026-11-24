"""Real Blender and FFmpeg round trip with a deterministic authoring fixture."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from film_worker import media, production
from film_worker.production import Production
from film_worker.settings import DEFAULTS, resolve_tools


class ProductionTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FILM_WORKER_TEST_MEDIA") == "1", "requires Blender, FFmpeg and FFprobe")
    def test_prepare_revise_finalize_from_frozen_artifacts(self):
        tools = resolve_tools(DEFAULTS)
        with tempfile.TemporaryDirectory(prefix="film-worker-integration-") as root:
            base = Path(root)
            src = base / "source.mp4"
            subprocess.run([tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "0.125",
                            "-c:v", "mpeg4", "-c:a", "aac", str(src)], check=True)
            paths = {"source_video": str(src)}
            for role in ("rules_bundle", "workflow_bundle"):
                artifact = base / (role + ".zip")
                with zipfile.ZipFile(artifact, "w") as bundle:
                    bundle.writestr("fixture.txt", "production fixture")
                paths[role] = str(artifact)
            full = os.environ.get("FILM_WORKER_TEST_FULL_SIZE") == "1"
            fw,fh,rw,rh = (3840,2160,1920,1080) if full else (1280,720,960,540)
            profile = dict(final_width=fw, review_landscape_limit=dict(width=rw,height=rh),
                           review_portrait_limit=dict(width=rh,height=rw), profile_hash="f"*64,
                           compatibility=dict(decoder_id="fixture"))

            def run(kind, previous, version, inputs, attempt=""):
                directory = base / ("attempt-" + kind + attempt)
                directory.mkdir()
                fixtures = {role: Path(path) for role, path in inputs.items()}
                artifacts = [dict(role=k, media_type="video/mp4" if k=="source_video" else "application/octet-stream")
                             for k in fixtures]
                order = dict(kind=kind, profile=profile, artifacts=artifacts, job_id="job1",
                             base_version=previous, target_version=version)
                spec = dict(input_manifest=order, input_manifest_sha256="0"*64)
                request = dict(assignment=spec, paths=inputs, tools=tools,
                               environment=dict(render_backend="cpu"), resources=str(base/"slots"),
                               cpu_threads=2, model_slots=1, render_slots=1)
                with patch.dict(os.environ, {"FILM_WORKER_FFMPEG":tools["ffmpeg"],
                                              "FILM_WORKER_FFPROBE":tools["ffprobe"]}), patch.object(Production, "author", author_fixture):
                    return Production(directory, request).run()

            first = run("prepare", None, 1, paths)["outputs"]
            orig = media.read_json(Path(first["version_manifest"]))
            self.assertEqual(orig["frame_count"], 3)
            self.assertEqual(orig["review_size"], dict(width=rw,height=rh))
            edit_inputs = {**paths, **{k:v for k,v in first.items() if k!="production_report"}}
            approval = base/"approval.json"
            feedback = base/"feedback.json"
            original_sha = hashlib.sha256(Path(first["version_manifest"]).read_bytes()).hexdigest()
            origin = dict(job_id="job1", base_version=1, base_manifest_sha256=original_sha,
                          original_annotator_id="alice", timeline_sha256=orig["timeline_sha256"])
            media.write_json(approval, dict(origin, scope="approved_subset", source_sha256=orig["source_sha256"],
                                            profile_hash=orig["profile_hash"], scene_sha256=orig["scene_sha256"],
                                            frame_count=3, frames=[]))
            frame = orig["frames"][1]
            media.write_json(feedback, dict(origin, review_size=orig["review_size"],
                    frames=[dict(number=2, review_signature=frame["review_signature"], note="调整站位", regions=[])]))
            edit_inputs.update(approval_manifest=str(approval), feedback_manifest=str(feedback))
            second = run("revise", 1, 2, edit_inputs)["outputs"]
            revised = media.read_json(Path(second["version_manifest"]))
            self.assertNotEqual(orig["frames"][1]["review_signature"], revised["frames"][1]["review_signature"])
            second_sha = hashlib.sha256(Path(second["version_manifest"]).read_bytes()).hexdigest()
            approved = dict(origin, base_version=2, base_manifest_sha256=second_sha,
                    scope="all_frames", source_sha256=revised["source_sha256"], profile_hash=revised["profile_hash"],
                    scene_sha256=revised["scene_sha256"], frame_count=3,
                    frames=[dict(number=f["number"], review_signature=f["review_signature"], approved_version=2)
                            for f in revised["frames"]])
            final_inputs = {**edit_inputs, **second}
            media.write_json(approval, {**approved, "frames": approved["frames"][:2]})
            with self.assertRaisesRegex(ValueError, "审批清单未覆盖全片"):
                run("finalize", 2, 2, final_inputs, "-incomplete")
            media.write_json(approval, approved)
            result = run("finalize", 2, 2, final_inputs)
            check = result["final_verification"]
            self.assertEqual(check["render_source"], "locked_scene_native_render")
            self.assertEqual(check["white_size"], dict(width=fw,height=fh))
            self.assertEqual(check["verified_review_frames"], 3)
            with zipfile.ZipFile(result["outputs"]["delivery_bundle"]) as archive:
                self.assertEqual(archive.read("target/source.mp4"), src.read_bytes())
                lines = archive.read("meta/video-technical.jsonl").splitlines()
                records = [json.loads(row) for row in lines]
                self.assertEqual([r["role"] for r in records], ["source_video", "white_video", "comparison_video"])
                self.assertEqual({r["sample_id"] for r in records}, {"job1"})
                for rendered in records[1:]:
                    track = rendered["audio"][0]
                    self.assertEqual((track["codec"], track["channels"], track["sample_rate"]), ("aac", 2, 48000))


def author_fixture(worker, source):
    preparing = worker.kind == "prepare"
    script = worker.root / "author.py"
    sound_file = worker.stage / "packed-reference.wav"
    if preparing:
        with wave.open(str(sound_file), "wb") as sound:
            sound.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            sound.writeframes(bytes(12000))
    script.write_text(f"""import bpy
s = bpy.context.scene
s.render.engine = "CYCLES"
s.cycles.samples = 1
s.cycles.use_denoising = False
s.frame_start = 1
s.frame_end = {worker.count}
if {preparing!r}:
    sequence = s.sequence_editor_create()
    strips = sequence.strips if hasattr(sequence, "strips") else sequence.sequences
    strip = strips.new_sound("packed reference", {str(sound_file)!r}, 1, 1)
    strip.sound.pack()
    strip.sound.filepath = "//packed-reference.wav"
else:
    subject = bpy.data.objects["Cube"]
    for frame, x in ((1, 0), (2, 1), (3, {int(worker.timeline["variable_rate"])})):
        subject.location.x = x
        subject.keyframe_insert(data_path="location", frame=frame)
bpy.context.preferences.filepaths.save_version = 0
bpy.ops.wm.save_as_mainfile(filepath={str(worker.stage / 'scene.blend')!r})
""",encoding="utf-8")
    command = [worker.tools["blender"], "-b", "--disable-autoexec"]
    if not preparing:
        command.append(str(worker.stage/"scene.blend"))
    worker.run_tool("fixture-author", [*command, "--python-exit-code", "1", "-P", str(script)])
    if preparing:
        sound_file.unlink()
    metadata = worker.stage / "meta.jsonl"
    metadata.write_text(json.dumps(dict(start_frame=1,end_frame=worker.count,description="fixture shot"))+"\n")
