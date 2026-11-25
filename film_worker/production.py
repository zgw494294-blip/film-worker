"""Translate a server work order into local media production."""
import contextlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import signal
import threading
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from . import blender_author, bundles, draft_review, media, model, quality, shot_draft
from .actor_guard import ACTOR_GEOMETRY
from .engineering_checks import EngineeringValidationError
from .production_helpers import check_rendered_frames, validate_meta, write_feedback
from .production_palette import prompt_table
from .protocol import digest
from .settings import atomic_json
from .state import slot

HERE = Path(__file__).resolve().parent
BODY_MODEL = HERE / "assets/基础模型.blend"
SKILL = HERE / "skills/movie-shot-workflow"
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
        self.profile = request.get("production") or self.order.get("profile") or {}
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

    def blender_command(self, *arguments):
        return [sys.executable, str(HERE / "blender_author.py"), self.tools["blender"], *arguments]

    def run_tool(self, name, command, *, stdin=None, env=None, frames=None, stage=None, engineering=False):
        logpath = self.root / (name + ".log")
        with contextlib.ExitStack() as stack:
            log = stack.enter_context(logpath.open("ab"))
            offset = log.tell()
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
            if engineering and child.returncode == 1:
                with logpath.open("rb") as log:
                    log.seek(max(offset, logpath.stat().st_size - 4096))
                    report = re.search(r"(?m)^(?:engineering_checks\.)?EngineeringValidationError: (.+)$",
                                       log.read().decode("utf-8", "replace"))
                if report:
                    raise EngineeringValidationError(report[1])
            raise RuntimeError(f"{name} 退出码 {child.returncode}，详见本机日志")

    def run(self):
        if not quality.may_author(self.profile, self.kind):
            raise ValueError("正式批次要求人工白模，不领取 Codex 建模或返修")
        if "export_quality" in self.order and (
                self.kind != "finalize" or self.order["export_quality"] not in ("1080p", "4k")):
            raise ValueError("成片质量仅适用于出片工单，可选 1080p 或 4k")
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
        width = (size(media.read_json(self.sources["version_manifest"])["final_size"])[0]
                 if self.kind != "prepare" else self.profile.get("final_width", 3840))
        timeline = media.prepare_source(self.root, source, width)
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
            frozen = media.read_json(self.timeline_file)
            if any(frozen.get(k) != v for k, v in wire.items() if k != "decoder_id"):
                raise ValueError("本机重建的源片时间线与冻结版本不同")
            self.load_previous()
        self.timeline_sha = digest(self.timeline_file)
        if self.kind == "finalize":
            return self.finalize(source)
        return self.create_review(source)

    def load_previous(self):
        base = media.read_json(self.sources["version_manifest"])
        if (base["job_id"], base["version"], base["frame_count"], base["source_sha256"],
                base["timeline_sha256"]) != (
                self.order["job_id"], self.order["base_version"], self.count, self.timeline["source_sha256"],
                digest(self.sources["source_timeline"])):
            raise ValueError("基版的作业、版号、原片或时间线不符")
        if quality.policy(self.profile) == "human_required" and base.get("production_method") != "human":
            raise ValueError("正式出片缺少已审核的人工作品来源")
        self.previous = base
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
                "job_id", "source_sha256", "timeline_sha256", "review_size", "final_size", "frames")):
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
                        scene_sha256=base["scene_sha256"], frame_count=self.count,
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
                if (self.prior_scene / ACTOR_GEOMETRY).is_file():
                    shutil.copyfile(self.prior_scene / ACTOR_GEOMETRY, self.stage / ACTOR_GEOMETRY)
            try:
                self.author_and_validate(source, scene, meta)
            except EngineeringValidationError as exc:
                attempted = self.stage / ".engineering-repair-attempted"
                if attempted.exists():
                    raise
                attempted.write_text(str(exc) + "\n", encoding="utf-8")
                self.author_and_validate(source, scene, meta, repair=str(exc))
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
                      timeline_sha256=self.timeline_sha, scene_sha256=scene_sha,
                      review_size=dimensions(self.review_size), final_size=dimensions(self.final_size), frames=frames)
        if "profile_hash" in self.profile:
            common["profile_hash"] = self.profile["profile_hash"]
        review_bundle = self.output / "review.zip"
        bundles.build(review_root, dict(common, target_version=self.order["target_version"]), review_bundle)
        report = self.output / "production.json"
        records = self.stage / "制作记录.md"
        details = records.read_text(encoding="utf-8")[:30000] if records.is_file() else ""
        media.write_json(report, dict(kind=self.kind, frame_count=self.count, environment=self.env, notes=details))
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
        if (self.stage / ACTOR_GEOMETRY).is_file():
            shutil.copyfile(self.stage / ACTOR_GEOMETRY, portable / ACTOR_GEOMETRY)
        return portable

    def author_and_validate(self, source, scene, meta, *, repair=None):
        self.progress("repair_engineering" if repair else "waiting_for_codex")
        resources = Path(self.request["resources"])
        with slot(resources, "codex", self.request["model_slots"]):
            if repair is None:
                self.author(source)
            else:
                self.author(source, repair=repair)
        if not scene.is_file():
            raise EngineeringValidationError("缺少可编辑的 Blender 工程 scene.blend")
        try:
            validate_meta(meta, self.count)
        except ValueError as exc:
            raise EngineeringValidationError(str(exc)) from exc
        self.progress("validate_scene")
        with slot(resources, "blender", self.request["render_slots"]):
            self.run_tool("blender-prepare", self.blender_command("-b", "--disable-autoexec", str(scene),
                          "--python-exit-code", "1", "-P", str(HERE / "prepare_render.py")),
                          env=dict(os.environ, MOVIE_JOB_TIMELINE=str(self.root / "intake/timeline.json"),
                                   MOVIE_BUILTIN_RULES="1"), engineering=True)

    def draft_commands(self, source):
        compile_cmd = shot_draft.compile_command(self.blender_command(), self.stage)
        review_cmd = [sys.executable, str(HERE / "draft_review.py"), str(self.stage), str(source),
                      self.tools["blender"], str(self.request["cpu_threads"]),
                      "--ffmpeg", self.tools["ffmpeg"], "--ffprobe", self.tools["ffprobe"]]
        return compile_cmd, review_cmd

    def author_prompt(self, source, repair=None):
        quote = subprocess.list2cmdline if os.name == "nt" else shlex.join
        compile_cmd, review_cmd = self.draft_commands(source)
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        sop = (SKILL / "references/制作SOP.md").read_text(encoding="utf-8")
        phase = f"""这是 Codex 内部试制首制。先核对完整原片、编号帧和 镜头参考，使用连续帧修正候选切点。
完成 shot-plan.json 后用下列命令编译出完整 scene.blend 与 meta.jsonl：
{quote(compile_cmd)}
可追加 --preview 与帧号快速试渲。随后按原时间线编码全部帧的首版自检对比：
{quote(review_cmd)}
查看整条自检片。无法播放时从编码后的视频解出连续画面，逐镜复查，发现偏差就修正、重编、重新编码再检查。
镜头表规范如下。
{(SKILL / 'references/镜头表.md').read_text(encoding='utf-8')}
""" if self.kind == "prepare" else """这是 Codex 内部试制返修。当前目录已有基版场景、镜头信息、返修单和圈选预览。
先读 feedback-summary.json，再按需要检查 feedback.json 原文、圈选图及连续源帧。按帧号核对受影响的动作范围和相邻帧，修改 scene.blend 及需要更新的 meta.jsonl。
圈选坐标相对于整张上下对比图，上方原片、下方白模。补建网格使用包内 production_palette.apply_palette 并保留原来的人体几何及其配色属性。
"""
        technical = (f"\n工程核验失败，工作台要求做一次技术修复。实际错误：{repair}\n"
                     "仅修复此问题及直接受影响的文件。首制修改后重新编译、编码和检查。") if repair else ""
        return f"""原片 {source}。编号源帧 {self.frames}。本轮只写入 {self.stage}。
使用包内人体白模 {BODY_MODEL}，以及随本提示附带的制作规则、镜头表与固定配色。制作帧从 1 到 {self.count}，
沿用原片显示时间，工程画幅 {self.final_size}。全片渲染、签名比对、送审和成片由执行端处理。
Blender 启动只能用后台前缀 {quote(self.blender_command())}，追加 -b --disable-autoexec --python-exit-code 1。
检查投影与预览也沿用此命令。Windows PowerShell 中为带引号的可执行文件添加调用符 &。
所有制作和试渲统一用 Cycles。禁止在补建脚本或工程中切换到 Eevee、Workbench。
如需从自检 MP4 读取编号帧，FFmpeg 路径为 {quote([self.tools["ffmpeg"]])}。
提交本轮 制作记录.md，写明切点、可见动作、核对过的帧段和需要人工判断的地方。
禁止用源帧贴图、加入外部素材、重塑甲方人体或访问历史工单和用户目录。

{phase}
{technical}
{skill}
{sop}

## 工作台内置配色
{prompt_table()}
"""

    def run_model(self, text, name):
        prompt = self.root / (name + "-prompt.txt")
        prompt.write_text(text, encoding="utf-8")
        overrides = dict(self.profile)
        if self.kind == "prepare" and not overrides.get("reasoning_effort"):
            overrides["reasoning_effort"] = "medium"
        with blender_author.background_environment(self.tools["blender"]) as env:
            env.pop("FILM_WORKER_TOKEN", None)
            command = model.options(self.tools["codex"], overrides, self.stage, sandbox="workspace-write",
                                    blender_config=env.get("BLENDER_USER_CONFIG"))
            command += ["-o", str(self.root / (name + "-receipt.txt")), "-"]
            self.run_tool(name, command, stdin=prompt, env=env)
        with (self.root / (name + ".log")).open(encoding="utf-8", errors="replace") as log:
            actual = model.evidence(log.read(32768))
        self.env.update(actual, model_evidence="observed" if all(actual.values()) else "unknown")

    def author(self, source, *, repair=None):
        self.progress("repair_engineering" if repair else "codex_author")
        if self.kind == "prepare" and not (self.stage / "draft-context.json").is_file():
            if not BODY_MODEL.is_file():
                raise RuntimeError("发布包缺少基础模型.blend，请恢复完整的执行端")
            shot_draft.prepare(self.stage, source, self.root / "intake", self.timeline, BODY_MODEL)
        prompt = self.author_prompt(source, repair)
        self.run_model(prompt, "codex-repair" if repair else "codex")
        if self.kind == "prepare":
            self.complete_draft(source, prompt)

    def complete_draft(self, source, prompt):
        if not (self.stage / "shot-plan.json").is_file():
            raise EngineeringValidationError("首版缺少 shot-plan.json")
        compile_cmd, review_cmd = self.draft_commands(source)
        resources = Path(self.request["resources"])
        record = self.stage / "制作记录.md"
        if not shot_draft.current(self.stage):
            with slot(resources, "blender", self.request["render_slots"]):
                self.run_tool("compile-draft", compile_cmd, engineering=True)
        current = draft_review.current(self.stage)
        if current and record.is_file() and record.read_text(encoding="utf-8").strip():
            return
        self.progress("draft_review")
        if not current:
            with slot(resources, "blender", self.request["render_slots"]):
                self.run_tool("draft-review", review_cmd, engineering=True)
        self.run_model(prompt + "\n复查当前首版自检对比片，修正后重新编码并更新制作记录。", "codex-self-review")
        if not (shot_draft.current(self.stage) and draft_review.current(self.stage)
                and record.is_file() and record.read_text(encoding="utf-8").strip()):
            raise EngineeringValidationError("首版缺少本工程对应的自检对比片或制作记录")

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
            args = self.blender_command("-b", "--disable-autoexec", str(scene), "--python-exit-code", "1", "-P",
                    str(HERE / "render_frames.py"), "--", str(frames), option, *map(str,target),
                    str(self.request["cpu_threads"]))
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
        selected = self.order.get("export_quality")
        if selected == "4k" and self.final_size[0] != 3840:
            raise ValueError("4K 出片需要宽 3840 的制作档案")
        output_size = self.final_size
        if selected == "1080p":
            output_size = tuple(media.even(axis) for axis in media.review_size(self.timeline))
        _, approved = self.approval()
        self.progress("verify_review", 0, self.count)
        for n, frame in approved.items():
            white = media.rgb(self.prior_review / bundles.safe_path(frame["white_png_path"]))
            pair = media.comparison(self.frames, white, n)
            if white.size != self.review_size or media.signature(pair, self.timeline, n) != frame["review_signature"]:
                raise ValueError(f"第 {n} 帧审核像素签名核验失败")
            self.progress("verify_review", n, self.count)
        approved_native = self.review_size == output_size
        if approved_native:
            def render_white(n):
                return media.rgb(self.prior_review / bundles.safe_path(approved[n]["white_png_path"]))
        else:
            rendered = self.render(self.scene_file, output_size, "final")
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
        compare_size = output_size[0], output_size[1]*2
        for name, video, extent, read in (
                ("white", white_path, output_size, render_white),
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
                     timeline_sha256=self.timeline_sha, final_size=dimensions(output_size))
        archive = self.output / "delivery.zip"
        bundles.build(delivery, index, archive)
        self.env.update({k: self.previous["environment"].get(k) for k in ("codex_model","codex_provider","reasoning_effort")},
                        model_evidence="inherited")
        verification = {k: index[k] for k in ("version_manifest_sha256", "approval_manifest_sha256", "timeline_sha256")}
        verification.update(scene_sha256=digest(self.scene_file), frame_count=self.count, verified_review_frames=self.count,
                            white_frame_count=self.count, comparison_frame_count=self.count, pts_verified=True,
                            audio_verified=True, white_size=dimensions(output_size), comparison_size=dimensions(compare_size),
                            render_source="approved_native_white" if approved_native else "locked_scene_native_render")
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
