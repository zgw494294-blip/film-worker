"""Translate a server work order into local media production."""
import contextlib
import json
import mimetypes
import os
import shutil
import signal
import threading
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from . import bundles, media, model, quality
from .production_helpers import check_rendered_frames, validate_meta, write_feedback
from .protocol import digest
from .settings import atomic_json
from .state import slot

HERE = Path(__file__).resolve().parent
VIDEO_SUFFIXES = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv",
                  "video/webm": ".webm", "video/x-msvideo": ".avi"}


def size(value):
    return value["width"], value["height"]


def dimensions(value):
    return dict(zip(("width", "height"), value, strict=True))


def to_wire(timeline, compatibility):
    endpoints = timeline["pts"] + [timeline["end"]]
    base = Fraction(timeline["time_base"])
    return dict(schema_version=1, source_sha256=timeline["source_sha256"],
                decoder_id=compatibility["decoder_id"], source_size=dimensions(timeline["source_size"]),
                display_size=dimensions(timeline["display_size"]), time_base=f"{base.numerator}/{base.denominator}",
                end_pts=timeline["end"], frame_count=len(timeline["pts"]), average_fps=timeline["fps"],
                frames=[dict(number=i, pts=a, duration_pts=b-a) for i, (a, b) in enumerate(
                    zip(endpoints, endpoints[1:]), 1)], variable_rate=timeline["variable_rate"],
                audio_stream_index=timeline["audio_index"], audio_offset=timeline["audio_offset"])


def review_dimensions(timeline, profile):
    w, h = timeline["width"], timeline["height"]
    limit = profile["review_landscape_limit" if w >= h else "review_portrait_limit"]
    scale = min(Fraction(1), Fraction(limit["width"], w), Fraction(limit["height"], h))
    return max(1, round(w * scale)), max(1, round(h * scale))


class Production:
    def __init__(self, directory, request):
        self.root = directory
        self.request = request
        self.order = request["assignment"]["input_manifest"]
        self.kind = self.order["kind"]
        self.profile = self.order["profile"]
        self.env = request["environment"].copy()
        self.tools = request["tools"]
        self.sources = {key: Path(path) for key, path in request["paths"].items()}
        self.stage = directory / "scene"
        self.stage.mkdir(exist_ok=True)
        self.output = directory / "out"
        self.output.mkdir(exist_ok=True)
        self.progress_path = directory / "progress.json"
        self.count = 0

    def progress(self, stage, count=0, total=None):
        atomic_json(self.progress_path, dict(stage=stage, completed=count, total=total))

    def run_tool(self, name, command, *, stdin=None, env=None, frames=None, stage=None):
        logpath = self.root / (name + ".log")
        with contextlib.ExitStack() as stack:
            log = stack.enter_context(logpath.open("ab"))
            prompt = stack.enter_context(stdin.open("rb")) if stdin else subprocess.DEVNULL
            child = subprocess.Popen(command, cwd=self.stage, env=env, stdin=prompt, stdout=log,
                                     stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            while child.poll() is None:
                if frames is not None:
                    self.progress(stage, sum(1 for p in frames.glob("*.png") if p.stem.isdigit()), self.count)
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if child.returncode:
            raise RuntimeError(f"{name} 退出码 {child.returncode}，详见本机日志")

    def run(self):
        if not quality.may_author(self.profile, self.kind):
            raise ValueError("正式批次要求人工白模，不领取 Codex 建模或返修")
        artifacts = {item["role"]: item for item in self.order["artifacts"]}
        self.progress("prepare_source")
        media_type = artifacts["source_video"]["media_type"]
        original_suffix = VIDEO_SUFFIXES.get(media_type) or mimetypes.guess_extension(media_type) or ".video"
        source = self.root / ("source" + original_suffix)
        source_sha = artifacts["source_video"].get("sha256") or digest(self.sources["source_video"])
        if not source.exists() or digest(source) != source_sha:
            temporary = source.with_suffix(".partial")
            shutil.copyfile(self.sources["source_video"], temporary)
            if digest(temporary) != source_sha:
                raise ValueError("源片校验失败")
            temporary.replace(source)
        timeline = media.prepare_source(self.root, source, self.profile["final_width"])
        self.timeline = timeline
        self.count = len(timeline["pts"])
        self.final_size = timeline["width"], timeline["height"]
        self.review_size = review_dimensions(timeline, self.profile)
        self.frames = self.root / "intake/frames"
        wire = to_wire(timeline, self.profile["compatibility"])
        timeline["time_base"] = wire["time_base"]
        media.write_json(self.root / "intake/timeline.json", timeline)
        if self.kind == "prepare":
            self.timeline_file = self.output / "timeline.json"
            media.write_json(self.timeline_file, wire)
            self.previous = None
        else:
            self.timeline_file = self.sources["source_timeline"]
            if media.read_json(self.timeline_file) != wire:
                raise ValueError("本机重建的源片时间线与冻结版本不同")
            self.load_previous()
        self.timeline_sha = digest(self.timeline_file)
        if self.kind == "finalize":
            return self.finalize(source)
        return self.create_review(source)

    def load_previous(self):
        base = media.read_json(self.sources["version_manifest"])
        if (base["job_id"], base["version"], base["frame_count"], base["source_sha256"],
                base["timeline_sha256"], base["profile_hash"]) != (
                self.order["job_id"], self.order["base_version"], self.count, self.timeline["source_sha256"],
                digest(self.sources["source_timeline"]), self.profile["profile_hash"]):
            raise ValueError("基版的作业、版号、原片、时间线或档案不符")
        if quality.policy(self.profile) == "human_required" and base.get("production_method") != "human":
            raise ValueError("正式出片缺少已审核的人工作品来源")
        self.previous = base
        if self.kind == "finalize":
            self.review_size = size(base["review_size"])
        if size(base["final_size"]) != self.final_size:
            raise ValueError("基版交付画幅不符")
        self.prior_scene = self.root / "prior-scene"
        self.prior_review = self.root / "prior-review"
        for target in (self.prior_scene, self.prior_review):
            if target.exists():
                shutil.rmtree(target)
        scene = bundles.extract(self.sources["scene_bundle"], self.prior_scene, indexed=True)
        review = bundles.extract(self.sources["review_bundle"], self.prior_review, indexed=True)
        self.scene_file = self.prior_scene / "scene.blend"
        if (digest(self.scene_file), scene["scene_sha256"], review["scene_sha256"],
                digest(self.sources["scene_bundle"]), digest(self.sources["review_bundle"]),
                digest(self.sources["shot_metadata"])) != (
                base["scene_sha256"], base["scene_sha256"], base["scene_sha256"], base["scene_bundle_sha256"],
                base["review_bundle_sha256"], base["shot_metadata_sha256"]):
            raise ValueError("基版工程或审片文件已变更")
        if any(review.get(k) != base.get(k) for k in (
                "job_id", "source_sha256", "timeline_sha256", "profile_hash", "review_size", "final_size", "frames")):
            raise ValueError("基版审核索引不匹配")
        if [f["number"] for f in base["frames"]] != list(range(1, self.count + 1)):
            raise ValueError("审核帧索引不连续")
        for frame, pts in zip(base["frames"], self.timeline["pts"], strict=True):
            if frame["pts"] != pts:
                raise ValueError("审核索引与 PTS 不符")
            for role in ("pair_jpeg_path", "white_png_path"):
                file = self.prior_review / bundles.safe_path(frame[role])
                if not file.is_file():
                    raise ValueError("审核帧图片缺失")

    def approval(self):
        approvals = media.read_json(self.sources["approval_manifest"])
        base = self.previous
        required = dict(job_id=self.order["job_id"], base_version=base["version"],
                        base_manifest_sha256=digest(self.sources["version_manifest"]),
                        source_sha256=base["source_sha256"], timeline_sha256=base["timeline_sha256"],
                        profile_hash=base["profile_hash"], scene_sha256=base["scene_sha256"], frame_count=self.count,
                        scope="all_frames" if self.kind == "finalize" else "approved_subset")
        if any(approvals.get(k) != v for k, v in required.items()) or not approvals.get("original_annotator_id"):
            raise ValueError("审批清单不是当前基版的冻结审批")
        frames = self.verify_items(approvals["frames"])
        if self.kind == "finalize" and len(frames) != self.count:
            raise ValueError("审批清单未覆盖全片")
        return approvals["original_annotator_id"], frames

    def verify_items(self, items):
        frames = {frame["number"]: frame for frame in self.previous["frames"]}
        result = {}
        for item in items:
            n = item["number"]
            if n in result or n not in frames or item["review_signature"] != frames[n]["review_signature"]:
                raise ValueError("标注或审批记录与基版逐帧签名不符")
            result[n] = frames[n]
        return result

    def create_review(self, source):
        needed = ("rules_bundle", "workflow_bundle")
        for role in needed:
            local = self.root / role
            if local.exists():
                shutil.rmtree(local)
            bundles.extract(self.sources[role], local, indexed=False)
        if self.previous:
            annotator, approved = self.approval()
            feedback = media.read_json(self.sources["feedback_manifest"])
            conditions = dict(job_id=self.order["job_id"], base_version=self.previous["version"],
                              original_annotator_id=annotator, timeline_sha256=self.timeline_sha,
                              base_manifest_sha256=digest(self.sources["version_manifest"]),
                              review_size=self.previous["review_size"])
            marked = self.verify_items(feedback["frames"])
            if not marked or marked.keys() & approved.keys() or any(feedback.get(k) != v for k,v in conditions.items()):
                raise ValueError("返修单与基版或原标注员不一致")
            # Feedback helper expects chronological JPEG names.
            images = self.root / "feedback-base/pairs"
            images.mkdir(parents=True, exist_ok=True)
            for n, frame in marked.items():
                shutil.copyfile(self.prior_review / bundles.safe_path(frame["pair_jpeg_path"]), images / f"{n:04d}.jpg")
            write_feedback(self.stage, images.parent, self.previous["version"], feedback["frames"])
        scene = self.stage / "scene.blend"
        meta = self.stage / "meta.jsonl"
        prepared = self.root / "prepared.json"
        expected = self.request["assignment"]["input_manifest_sha256"]
        saved = media.read_json(prepared) if prepared.is_file() else {}
        hashes = {"input": expected, "scene": digest(scene) if scene.is_file() else "",
                  "meta": digest(meta) if meta.is_file() else ""}
        if saved and all(saved.get(k) == v for k, v in hashes.items()):
            self.env.update(saved["environment"])
        else:
            if self.previous:
                shutil.copyfile(self.scene_file, scene)
                shutil.copyfile(self.sources["shot_metadata"], meta)
            self.progress("waiting_for_codex")
            with slot(Path(self.request["resources"]), "codex", self.request["model_slots"]):
                self.author(source)
            self.progress("validate_scene")
            with slot(Path(self.request["resources"]), "blender", self.request["render_slots"]):
                self.run_tool("blender-prepare", [self.tools["blender"], "-b", "--disable-autoexec", str(scene),
                                                "--python-exit-code", "1", "-P", str(HERE / "prepare_render.py")],
                              env=dict(os.environ, MOVIE_JOB_TIMELINE=str(self.root / "intake/timeline.json")))
            validate_meta(meta, self.count)
            atomic_json(prepared, dict(input=expected, scene=digest(scene), meta=digest(meta), environment=self.env))
        raw_meta = (self.stage / "meta.jsonl").read_text(encoding="utf-8")
        metadata = self.output / "meta.jsonl"
        with metadata.open("w", encoding="utf-8") as f:
            for line in raw_meta.splitlines():
                if line.strip():
                    shot = json.loads(line)
                    f.write(json.dumps({k: shot[k] for k in ("start_frame","end_frame","description")}, ensure_ascii=False)+"\n")
        scene_sha = digest(scene)
        scene_bundle = self.output / "scene.zip"
        bundles.build(self.stage_bundle(scene), dict(schema_version=1, scene_path="scene.blend", scene_sha256=scene_sha),
                      scene_bundle)
        rendered = self.render(scene, self.review_size, "review")
        review_root = self.root / "review-bundle"
        (review_root / "pairs").mkdir(parents=True, exist_ok=True)
        (review_root / "white").mkdir(exist_ok=True)
        frames = []
        prior = {row["number"]: row for row in self.previous["frames"]} if self.previous else {}
        for i, pts in enumerate(self.timeline["pts"], 1):
            self.progress("compare_review", i, self.count)
            png = media.frame_path(rendered, i)
            white = media.rgb(png)
            pair = media.comparison(self.frames, white, i)
            signature = media.signature(pair, self.timeline, i)
            jpg_name, png_name = f"pairs/{i:04d}.jpg", f"white/{i:04d}.png"
            target = review_root / jpg_name
            if i in prior and prior[i]["review_signature"] == signature:
                shutil.copyfile(self.prior_review / bundles.safe_path(prior[i]["pair_jpeg_path"]), target)
            else:
                pair.save(target, quality=94, subsampling=0)
            shutil.copyfile(png, review_root / png_name)
            frames.append(dict(number=i, pts=pts, review_signature=signature, pair_jpeg_path=jpg_name, white_png_path=png_name))
        common = dict(schema_version=1, job_id=self.order["job_id"], source_sha256=self.timeline["source_sha256"],
                      timeline_sha256=self.timeline_sha, profile_hash=self.profile["profile_hash"], scene_sha256=scene_sha,
                      review_size=dimensions(self.review_size), final_size=dimensions(self.final_size), frames=frames)
        review_bundle = self.output / "review.zip"
        bundles.build(review_root, dict(common, target_version=self.order["target_version"]), review_bundle)
        report = self.output / "production.json"
        media.write_json(report, dict(kind=self.kind, frame_count=self.count, environment=self.env))
        manifest = self.output / "version.json"
        media.write_json(manifest, dict(common, version=self.order["target_version"], frame_count=self.count,
                                        production_method="codex_trial", scene_bundle_sha256=digest(scene_bundle), review_bundle_sha256=digest(review_bundle),
                                        shot_metadata_sha256=digest(metadata), production_report_sha256=digest(report),
                                        input_manifest_sha256=self.request["assignment"]["input_manifest_sha256"],
                                        environment=self.env))
        outputs = dict(scene_bundle=scene_bundle, shot_metadata=metadata, review_bundle=review_bundle,
                       version_manifest=manifest, production_report=report)
        if self.kind == "prepare":
            outputs["source_timeline"] = self.timeline_file
        return dict(outputs={k: str(v) for k,v in outputs.items()}, environment=self.env)

    def stage_bundle(self, scene):
        portable = self.root / "packed-scene"
        portable.mkdir(exist_ok=True)
        shutil.copyfile(scene, portable / "scene.blend")
        return portable

    def author(self, source):
        self.progress("codex_author")
        p = self.profile
        blender = subprocess.list2cmdline([sys.executable, str(HERE / "blender_author.py"), self.tools["blender"]])
        if os.name != "nt":
            import shlex
            blender = shlex.join([sys.executable, str(HERE / "blender_author.py"), self.tools["blender"]])
        prompt = self.root / "prompt.txt"
        prompt.write_text(f"""内部试制电影白模。先查看原片和代表帧，制作可编辑的 Blender 场景，后续由人逐帧审片。
原片 {source}。编号源帧 {self.frames}。规则目录 {self.root/'rules_bundle'}。参考工作流 {self.root/'workflow_bundle'}。
输出到当前工作目录：scene.blend 和 meta.jsonl。镜头说明每行一个对象，字段 start_frame、end_frame、description，
连续覆盖全部 {self.count} 帧。Blender 帧号为 1–{self.count}，输出比例对应 {self.final_size}。
若当前目录已有 scene.blend、meta.jsonl、feedback.json，按圈选图及逐帧原意见返修。检查意见前后相邻画面以及全片同类问题。
优先保证构图、场景和动作的时空对应。不得改动输入文件，不得交付用原始视频做贴图的平面。
只用无界面的 Blender，启动前缀 {blender}，传 -b 和 --python-exit-code 1。最终工程内打包使用的外部资源。
{quality.REVIEW_CHECKLIST}""", encoding="utf-8")
        command = model.options(self.tools["codex"], p, self.stage, sandbox="workspace-write")
        command += ["-o", str(self.root / "codex-receipt.txt"), "-"]
        self.run_tool("codex", command, stdin=prompt)
        with (self.root / "codex.log").open(encoding="utf-8", errors="replace") as log:
            actual = model.evidence(log.read(32768))
        for field, value in actual.items():
            if value and value != p[field]:
                raise ValueError(f"实际模型参数 {field} 与冻结档案不匹配")
        if all(actual.values()):
            self.env.update(actual, model_evidence="observed")
        else:
            self.env.update(actual, model_evidence="unknown")

    def render(self, scene, target, purpose):
        self.progress("waiting_for_blender")
        with slot(Path(self.request["resources"]), "blender", self.request["render_slots"]):
            return self.render_locked(scene, target, purpose)

    def render_locked(self, scene, target, purpose):
        backend = self.env["render_backend"]
        options = [backend, "cpu"] if backend != "cpu" else ["cpu"]
        scene_sha = digest(scene)
        for option in options:
            frames = self.root / f"{purpose}-{option}"
            recorded = frames / "checkpoint.json"
            tag = dict(scene_sha256=scene_sha, dimensions=list(target),
                       renderer=self.tools["blender"], blender=self.env.get("compatibility", {}).get("blender"),
                       count=self.count, backend=option)
            if frames.exists() and (not recorded.exists() or media.read_json(recorded) != tag):
                shutil.rmtree(frames)
            atomic_json(recorded, tag)
            check_rendered_frames(frames, target, self.count, complete=False)
            args = [self.tools["blender"], "-b", "--disable-autoexec", str(scene), "--python-exit-code", "1", "-P",
                    str(HERE / "render_frames.py"), "--", str(frames), option, *map(str,target),
                    str(self.request["cpu_threads"])]
            self.progress(f"render_{purpose}_{option}", 0, self.count)
            try:
                self.run_tool(f"blender-{purpose}-{option}", args, frames=frames, stage=f"render_{purpose}_{option}")
                check_rendered_frames(frames, target, self.count, complete=True)
                self.env["render_backend"] = option
                return frames
            except (ValueError, RuntimeError):
                if option == "cpu":
                    raise

    def finalize(self, source):
        _, approved = self.approval()
        self.progress("verify_review", 0, self.count)
        for n, frame in approved.items():
            white = media.rgb(self.prior_review / bundles.safe_path(frame["white_png_path"]))
            pair = media.comparison(self.frames, white, n)
            if white.size != self.review_size or media.signature(pair, self.timeline, n) != frame["review_signature"]:
                raise ValueError(f"第 {n} 帧审核像素签名核验失败")
            self.progress("verify_review", n, self.count)
        legacy = self.review_size == self.final_size
        if legacy:
            def render_white(n):
                return media.rgb(self.prior_review / bundles.safe_path(approved[n]["white_png_path"]))
        else:
            rendered = self.render(self.scene_file, self.final_size, "final")
            def render_white(n):
                return media.rgb(media.frame_path(rendered, n))
        delivery = self.root / "delivery"
        for folder in ("target", "white_model", "comparison", "model_3d", "meta"):
            (delivery / folder).mkdir(parents=True, exist_ok=True)
        original = delivery / "target" / source.name
        shutil.copyfile(source, original)
        shutil.copyfile(self.scene_file, delivery / "model_3d/scene.blend")
        shutil.copyfile(self.sources["shot_metadata"], delivery / "meta/scene.jsonl")
        white_path, compare_path = delivery / "white_model/white.mp4", delivery / "comparison/comparison.mp4"
        compare_size = self.final_size[0], self.final_size[1]*2
        for name, video, extent, read in (
                ("white", white_path, self.final_size, render_white),
                ("comparison", compare_path, compare_size, lambda n: media.comparison(self.frames, render_white(n), n))):
            self.progress("encode_" + name)
            silent = self.root / f"{name}-silent.mp4"
            silent.unlink(missing_ok=True)
            video.unlink(missing_ok=True)
            media.encode_images(silent, self.timeline, extent, read)
            media.add_audio(silent, video, self.timeline, source)
            media.validate_encoded(video, self.timeline, extent)
        self.progress("verify_technical")
        quality.media_report(delivery / "meta/video-technical.jsonl", self.order.get("sample_id", self.order["job_id"]),
                             [("source_video", original), ("white_video", white_path), ("comparison_video", compare_path)],
                             formal=quality.policy(self.profile) == "human_required")
        index = dict(schema_version=1, job_id=self.order["job_id"], version=self.order["target_version"],
                     version_manifest_sha256=digest(self.sources["version_manifest"]),
                     approval_manifest_sha256=digest(self.sources["approval_manifest"]),
                     timeline_sha256=self.timeline_sha, final_size=dimensions(self.final_size))
        archive = self.output / "delivery.zip"
        bundles.build(delivery, index, archive)
        self.env.update({k: self.previous["environment"].get(k) for k in ("codex_model","codex_provider","reasoning_effort")},
                        model_evidence="inherited")
        verification = {k: index[k] for k in ("version_manifest_sha256", "approval_manifest_sha256", "timeline_sha256")}
        verification.update(scene_sha256=digest(self.scene_file), frame_count=self.count, verified_review_frames=self.count,
                            white_frame_count=self.count, comparison_frame_count=self.count, pts_verified=True,
                            audio_verified=True, white_size=dimensions(self.final_size), comparison_size=dimensions(compare_size),
                            render_source="approved_native_white" if legacy else "locked_scene_native_render")
        return dict(environment=self.env, final_verification=verification,
                    outputs=dict(white_video=str(white_path), comparison_video=str(compare_path), delivery_bundle=str(archive)))


def run_child(root):
    if os.read(sys.stdin.fileno(), 1) != b"1":
        return 1

    def parent_watch():
        os.read(sys.stdin.fileno(), 1)
        if os.name == "nt":
            os._exit(1)
        os.killpg(os.getpgrp(), signal.SIGTERM)

    threading.Thread(target=parent_watch, daemon=True).start()
    request = media.read_json(root / "request.json")
    # Existing media routines use executable names on PATH. Their directories are pinned in the request.
    for tool in ("ffmpeg", "ffprobe"):
        os.environ["FILM_WORKER_" + tool.upper()] = request["tools"][tool]
    try:
        result = Production(root, request).run()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        atomic_json(root / "error.json", {"code": "input_invalid" if isinstance(exc, ValueError) else "render_failed",
                                         "message": type(exc).__name__})
        return 1
    atomic_json(root / "result.json", result)
    return 0
