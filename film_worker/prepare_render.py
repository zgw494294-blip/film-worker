"""Set the production timeline and validate the Blender project before rendering."""
import json
import os
import sys
from fractions import Fraction
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from actor_guard import ACTOR_GEOMETRY, assert_unchanged
from engineering_checks import EngineeringValidationError, require_cycles
from production_palette import ROLE_PROPERTY, apply_palette


# Blender can refer to its installed brushes even in an otherwise empty scene.
builtin_assets = Path(bpy.utils.system_resource("DATAFILES")).resolve()


def external_paths():
    packed = {Path(bpy.path.abspath(sound.filepath, library=sound.library)).resolve()
              for sound in bpy.data.sounds if sound.filepath and sound.packed_file and sound.packed_file.size}
    unpacked = {Path(bpy.path.abspath(sound.filepath, library=sound.library)).resolve()
                for sound in bpy.data.sounds if sound.filepath and not (sound.packed_file and sound.packed_file.size)}
    return [path for path in bpy.utils.blend_paths(absolute=True, packed=False)
            if path and Path(path).resolve() not in packed - unpacked
            and not (Path(path).is_file() and Path(path).resolve().is_relative_to(builtin_assets))]


def check_external_paths():
    external = external_paths()
    if external:
        raise EngineeringValidationError("交付工程仍引用外部素材：" + ", ".join(external[:5]))


timeline = json.loads(Path(os.environ["MOVIE_JOB_TIMELINE"]).read_text(encoding="utf-8"))
scene = bpy.context.scene
count = len(timeline["pts"])
if scene.camera is None or not any(obj.type == "MESH" and obj.data.polygons for obj in scene.objects):
    raise EngineeringValidationError("工程需要可编辑的三维场景和活动摄像机")
if scene.frame_start != 1 or scene.frame_end != count:
    raise EngineeringValidationError(f"工程帧范围须为 1–{count}")

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
if os.environ.get("MOVIE_BUILTIN_RULES") == "1":
    require_cycles(scene.render.engine)
    check_external_paths()
    if bpy.data.libraries:
        raise EngineeringValidationError("白模工程包含外部 Blender 链接库")
    images = [image.name for image in bpy.data.images
              if image.users and image.source not in {"VIEWER"} and image.type != "RENDER_RESULT"]
    if images:
        raise EngineeringValidationError("白模工程必须用原生几何和纯色材质，发现图片素材：" + ", ".join(images[:5]))
    actor_snapshot = Path(os.environ.get("MOVIE_ACTOR_GEOMETRY", Path(bpy.data.filepath).parent / ACTOR_GEOMETRY))
    if actor_snapshot.is_file():
        assert_unchanged(scene, json.loads(actor_snapshot.read_text(encoding="utf-8")))
    for obj in scene.objects:
        if ROLE_PROPERTY in obj:
            apply_palette(obj, obj[ROLE_PROPERTY])
bpy.ops.file.pack_all()
check_external_paths()
bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
