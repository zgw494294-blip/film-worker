"""Record geometry that belongs to the supplied human model."""
import hashlib
import json

if __package__:
    from .engineering_checks import EngineeringValidationError
else:
    from engineering_checks import EngineeringValidationError

ACTOR_GEOMETRY = ".actor-geometry.json"


def capture(scene):
    meshes = {}
    actors = {}
    for obj in scene.objects:
        if obj.type != "MESH" or not obj.get("movie_actor_body"):
            continue
        mesh = obj.data
        if mesh not in meshes:
            data = (
                [tuple(vertex.co) for vertex in mesh.vertices],
                [(tuple(face.vertices), face.material_index, face.use_smooth) for face in mesh.polygons],
            )
            meshes[mesh] = hashlib.sha256(json.dumps(data).encode()).hexdigest()
        actors[obj.name] = [meshes[mesh], list(obj.scale), list(obj.modifiers.keys()),
                            len(mesh.shape_keys.key_blocks) if mesh.shape_keys else 0]
    return actors


def assert_unchanged(scene, original):
    current = capture(scene)
    for name, geometry in original.items():
        if current.get(name) != geometry:
            raise EngineeringValidationError(f"{name} 的人体白模几何、比例或表面分区已改变，请恢复模型，仅修正动作")
