"""Prepare numbered shot references and the native Blender first-pass command."""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def candidate_starts(scores, dark_transition):
    starts = [1]
    for i, score in enumerate(scores[1:], 1):
        neighbors = scores[max(0, i - 8):i] + scores[i + 1:i + 9]
        # Low scores also occur during fast moves. Use isolated peaks only
        # when both actual frames are dark enough to suppress the cut score.
        if score >= 10 or (score >= 3 and score >= 2.5 * max(neighbors or [0]) and dark_transition(i + 1)):
            starts.append(i + 1)
    return starts


def detect(source, probe, count, frames):
    from PIL import Image

    def dark_transition(number):
        for n in (number - 1, number):
            with Image.open(frames / f"{n:04d}.png") as im:
                if im.convert("L").resize((1, 1), Image.Resampling.BOX).getpixel((0, 0)) > 40:
                    return False
        return True

    stream = next(s for s in probe["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic"))
    result = subprocess.run([os.getenv("FILM_WORKER_FFMPEG") or "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(source),
                             "-map", f"0:{stream['index']}", "-an", "-sn", "-dn", "-vf",
                             "scale=320:-2,scdet,metadata=mode=print:key=lavfi.scd.score:file=-",
                             "-f", "null", "-"], capture_output=True, text=True)
    hits = re.findall(r"frame:(\d+)[^\n]*\nlavfi\.scd\.score=([\d.]+)", result.stdout)
    if result.returncode or [int(n) for n, _ in hits] != list(range(count)):
        return [1], "自动切点不可用，请结合原片概览补切。"
    return candidate_starts([float(score) for _, score in hits], dark_transition), ""


def prepare(stage, source, intake, timeline, model):
    from PIL import Image, ImageDraw, ImageOps

    count = len(timeline["pts"])
    starts, note = detect(source, json.loads((intake / "probe.json").read_text()), count, intake / "frames")
    if 2 in starts:
        with Image.open(intake / "frames/0001.png") as im:
            if im.convert("L").getextrema()[1] <= 16:
                starts.remove(2)
                note = "片头第一帧近黑，已与后续画面一起展示。"
    refs = stage / "镜头参考"
    refs.mkdir(exist_ok=True)
    ranges = list(zip(starts, [v - 1 for v in starts[1:]] + [count]))
    for offset in range(0, len(ranges), 6):
        rows = ranges[offset:offset + 6]
        sheet = Image.new("RGB", (3 * 300, len(rows) * 184), "#181818")
        draw = ImageDraw.Draw(sheet)
        lifted = sheet.copy()
        lifted_draw = ImageDraw.Draw(lifted)
        has_dark_frame = False
        for row, (start, end) in enumerate(rows):
            for col, n in enumerate((start, (start + end) // 2, end)):
                with Image.open(intake / "frames" / f"{n:04d}.png") as frame:
                    thumb = ImageOps.contain(frame.convert("RGB"), (296, 160))
                x, y = col * 300, row * 184
                position = (x + (300 - thumb.width) // 2, y)
                sheet.paste(thumb, position)
                draw.text((x + 6, y + 162), f"F{n}   [{start}-{end}]", fill="white")
                dark = thumb.convert("L").resize((1, 1), Image.Resampling.BOX).getpixel((0, 0)) <= 40
                has_dark_frame |= dark
                lifted.paste(ImageOps.autocontrast(thumb, cutoff=.3, preserve_tone=True) if dark else thumb, position)
                lifted_draw.text((x + 6, y + 162), f"F{n}" + ("   lifted" if dark else ""), fill="white")
        sheet.save(refs / f"{offset // 6 + 1:03d}.jpg", quality=86)
        if has_dark_frame:
            lifted.save(refs / f"{offset // 6 + 1:03d}-暗部观察.jpg", quality=86)
    context = dict(timeline=str(intake / "timeline.json"), model=str(model),
                   palette_version=1, candidates=starts, note=note)
    (stage / "draft-context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    template = dict(objects={}, shots=[dict(start=a, end=b, description="", camera=[dict(f=a)],
                                           tracks={}) for a, b in ranges])
    (stage / "shot-plan.json").write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    return starts


def compile_command(blender_prefix, stage):
    return [*blender_prefix, "-b", "--disable-autoexec", "--python-exit-code", "1",
            "-P", str(HERE / "compile_shots.py"), "--", str(stage)]


def inputs_hash(stage):
    digest = hashlib.sha256((HERE / "compile_shots.py").read_bytes())
    digest.update((HERE / "production_palette.py").read_bytes())
    digest.update((HERE / "actor_guard.py").read_bytes())
    for name in ("draft-context.json", "shot-plan.json", "scene_details.py"):
        path = stage / name
        if path.is_file():
            digest.update(name.encode() + b"\x00" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def current(stage):
    stamp = stage / ".draft-inputs"
    return (stamp.is_file() and stamp.read_text().strip() == inputs_hash(stage)
            and (stage / "scene.blend").is_file() and (stage / "meta.jsonl").is_file())
