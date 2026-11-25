"""Built-in semantic body and scene materials for the white-model workflow."""
import re
from functools import lru_cache


PALETTE = {
    "lead": ("主角", "#d63e3d"),
    "second": ("第二角色", "#2e6ad8"),
    "third": ("第三角色", "#2f9e5a"),
    "fourth": ("第四角色", "#d9a013"),
    "foreground": ("前景", "#9a6c3b"),
    "background": ("背景", "#b8b8b8"),
    "extra": ("其他人物", "#787878"),
    "prop": ("普通道具", "#989898"),
}
ACTOR_ROLES = {"lead", "second", "third", "fourth", "extra"}
PROP_ROLES = {"foreground", "background", "prop"}
ROLE_PROPERTY = "movie_palette_role"


def prompt_table():
    return "\n".join(f"- `{role}` {label} `{hex_value}`"
                     for role, (label, hex_value) in PALETTE.items())


def color(hex_value):
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", hex_value):
        raise ValueError(f"颜色请用 #RRGGBB，收到 {hex_value!r}")
    srgb = [int(hex_value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb) + (1,)


@lru_cache(None)
def material(hex_value, template=None):
    import bpy

    mat = template.copy() if template is not None else bpy.data.materials.new(hex_value)
    mat.name = hex_value
    mat.diffuse_color = color(hex_value)
    if template is not None and not mat.use_nodes:
        return mat
    mat.use_nodes = True
    for node in mat.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            node.inputs["Base Color"].default_value = mat.diffuse_color
            if template is None:
                node.inputs["Roughness"].default_value = .78
    return mat


def apply_palette(obj, role):
    if not isinstance(role, str) or role not in PALETTE or obj.type != "MESH":
        raise ValueError(f"{obj.name} 的配色类别无效：{role!r}")
    template = obj.data.materials[0] if obj.get("movie_actor_body") and obj.data.materials else None
    body = material(PALETTE[role][1], template)
    if not obj.material_slots:
        obj.data.materials.append(body)
    obj.material_slots[0].link = "OBJECT"
    obj.material_slots[0].material = body
    obj[ROLE_PROPERTY] = role
