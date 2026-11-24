"""Set the production timeline and validate the Blender project before rendering."""
import json
import os
from fractions import Fraction
from pathlib import Path

import bpy


timeline = json.loads(Path(os.environ["MOVIE_JOB_TIMELINE"]).read_text(encoding="utf-8"))
scene = bpy.context.scene
count = len(timeline["pts"])
if scene.camera is None or not any(obj.type == "MESH" for obj in scene.objects):
    raise ValueError("工程需要可编辑的三维场景和活动摄像机")
if scene.frame_start != 1 or scene.frame_end != count:
    raise ValueError(f"工程帧范围须为 1–{count}")

scene.render.resolution_x = timeline["width"]
scene.render.resolution_y = timeline["height"]
scene.render.resolution_percentage = 100
scene.render.threads_mode = "AUTO"
scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1
fps = Fraction(timeline["fps"])
scene.render.fps = max(1, min(32767, round(fps)))
scene.render.fps_base = float(scene.render.fps / fps)
scene.render.film_transparent = False
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGB"
scene.render.image_settings.color_depth = "8"
scene.frame_set(1)
bpy.context.preferences.filepaths.save_version = 0
bpy.ops.file.pack_all()
# The factory scene refers to Blender's installed brush library even when the
# scene contains no external media. Those brushes ship with the runtime.
builtin_assets = Path(bpy.utils.system_resource("DATAFILES")).resolve()
external = [path for path in bpy.utils.blend_paths(absolute=True, packed=False)
            if path and not (Path(path).is_file() and Path(path).resolve().is_relative_to(builtin_assets))]
if external:
    raise ValueError("交付工程仍引用外部素材：" + ", ".join(external[:5]))
bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
