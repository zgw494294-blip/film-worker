"""Prepare native source frames and map them to a fixed-rate production timeline.

Requires ffprobe and ffmpeg on PATH. Both frame indices in the CSV start at 1.
"""
import argparse
import bisect
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path


def probe(source, *options):
    command = [os.environ.get("FILM_WORKER_FFPROBE", "ffprobe"), "-v", "error", *options, "-of", "json", str(source)]
    return json.loads(subprocess.check_output(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))


def measure(source):
    metadata = probe(source, "-show_streams", "-show_format")
    stream = next((s for s in metadata["streams"] if s["codec_type"] == "video"
                   and not s.get("disposition", {}).get("attached_pic")), None)
    if stream is None:
        raise ValueError("No video stream with moving pictures")
    index = str(stream["index"])
    frames = probe(source, "-select_streams", index, "-show_frames", "-show_entries",
                   "frame=pts,best_effort_timestamp,duration,pkt_duration")["frames"]
    if not frames:
        raise ValueError("No decoded video frames")
    pts = []
    for number, frame in enumerate(frames, 1):
        tick = frame.get("pts", frame.get("best_effort_timestamp"))
        if tick is None or (pts and int(tick) <= pts[-1]):
            raise ValueError(f"Frame {number} has missing or unordered presentation time")
        pts.append(int(tick))
    base = Fraction(stream["time_base"])
    if base <= 0:
        raise ValueError("Invalid time base")
    declared_start = int(stream.get("start_pts", pts[0]))
    last_duration = int(frames[-1].get("duration", frames[-1].get("pkt_duration", 0)))
    stream_end = declared_start + int(stream.get("duration_ts", 0))
    if stream_end > pts[-1]:
        end = stream_end
        duration_source = "stream start_pts plus duration_ts"
    elif last_duration > 0:
        end = pts[-1] + last_duration
        duration_source = "last frame pts plus duration"
    else:
        raise ValueError("Cannot determine the end of the last display frame")
    if end <= pts[-1]:
        raise ValueError("Stream end precedes the end of the last displayed frame")
    return metadata, stream, pts, base, (end - pts[0]) * base, duration_source


def digest(source):
    result = hashlib.sha256()
    with source.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_map(path, pts, base, count, fps):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["output_frame_1based", "output_time_s", "source_frame_1based",
                         "source_pts_ticks", "source_time_s", "source_elapsed_s"])
        for index in range(count):
            elapsed = index / fps
            position = bisect.bisect_right(pts, pts[0] + elapsed / base) - 1
            tick = pts[position]
            writer.writerow([index + 1, str(elapsed), position + 1, tick,
                             str(tick * base), str((tick - pts[0]) * base)])


def extract_source_frames(source, destination, stream_index, count):
    destination.mkdir()
    subprocess.run([os.environ.get("FILM_WORKER_FFMPEG", "ffmpeg"), "-nostdin", "-v", "error", "-i", str(source),
                    "-map", f"0:{stream_index}", "-an", "-fps_mode", "passthrough",
                    "-q:v", "2", "-start_number", "1", str(destination / "%04d.jpg")],
                   check=True)
    expected = {f"{n:04d}.jpg" for n in range(1, count + 1)}
    if {p.name for p in destination.glob("[0-9]*.jpg")} != expected:
        raise ValueError("Decoded JPEG count differs from presentation timestamp count")


def overview(source, destination, stream_index, count, *, scale_options=""):
    slots = min(count, 12)
    indices = [round(Fraction(n * (count - 1), max(1, slots - 1))) for n in range(slots)]
    expression = "+".join(f"eq(n,{i})" for i in indices)
    columns = min(slots, 4)
    rows = math.ceil(slots / columns)
    filters = f"select='{expression}',scale=320:-2{scale_options},tile={columns}x{rows}:nb_frames={slots}"
    subprocess.run([os.environ.get("FILM_WORKER_FFMPEG", "ffmpeg"), "-nostdin", "-v", "error", "-i", str(source),
                    "-map", f"0:{stream_index}", "-vf", filters, "-an", "-frames:v", "1",
                    "-q:v", "3", "-update", "1", str(destination)], check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not destination.is_file() or not destination.stat().st_size:
        raise ValueError("No contact sheet generated")
    return [index + 1 for index in indices]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--fps", default="24",
                        help="Production fps as a positive rational, or 'source' for the source average rate")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    output = args.output_dir
    if output.exists():
        parser.error(f"Output already exists: {output}")
    metadata, stream, pts, base, duration, reason = measure(source)
    if args.fps == "source":
        raw_rate = stream.get("avg_frame_rate")
        fps = len(pts) / duration if raw_rate in (None, "0/0", "0", "0/1") else Fraction(raw_rate)
    else:
        try:
            fps = Fraction(args.fps)
        except (ValueError, ZeroDivisionError):
            parser.error("--fps must be a positive rational number or 'source'")
    if fps <= 0:
        parser.error("The production frame rate must be positive")
    count = max(1, math.floor(duration * fps + Fraction(1, 2)))
    output.mkdir(parents=True, exist_ok=False)
    try:
        write_map(output / "frame-map.csv", pts, base, count, fps)
        extract_source_frames(source, output / "frames", stream["index"], len(pts))
        samples = overview(source, output / "overview.jpg", stream["index"], len(pts))
        metadata["timeline"] = {
            "source_sha256": digest(source), "video_stream_index": stream["index"],
            "source_frames_decoded": len(pts), "time_base": str(base),
            "first_pts_ticks": pts[0], "last_pts_ticks": pts[-1],
            "source_video_duration_s": str(duration), "duration_basis": reason,
            "output_fps": int(fps) if fps.denominator == 1 else str(fps), "output_frames": count,
            "output_duration_s": str(count / fps),
            "timing": f"Frame 1 starts at the first video PTS. At each frame index n divided by {fps} use the last "
                      "source frame whose onset is at or before this instant. Duration rounds "
                      "to the nearest frame, halves up, minimum one frame. Times are exact fractions.",
            "overview_source_frames_1based_row_major": samples,
        }
        with (output / "probe.json").open("w", encoding="utf-8") as file:
            json.dump(metadata, file, ensure_ascii=False, indent=2)
            file.write("\n")
    except BaseException:
        # On macOS removable volumes, deleting a file also removes its ._ companion.
        def handle_cleanup_error(function, path, exc_info):
            if not isinstance(exc_info[1], FileNotFoundError):
                raise exc_info[1]
        shutil.rmtree(output, onerror=handle_cleanup_error)
        raise
    print(f"{len(pts)} source frames -> {count} frames at {fps} fps in {output}")


if __name__ == "__main__":
    main()
