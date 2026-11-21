"""Render nine controlled start frames with Blender 4.3 in background mode."""
import math
from pathlib import Path
import sys

import bpy
from mathutils import Vector

OUT = Path(sys.argv[sys.argv.index("--") + 1])
CASES = {
    "voltage-indicator-95711": ((0.038, 0.056, 0.064), "VOLTAGE", 3.12),
    "signal-router-95721": ((0.044, 0.051, 0.077), "SIGNAL", 3.35),
    "interval-controller-95731": ((0.051, 0.062, 0.043), "INTERVAL", 2.95),
}


def material(name, color, roughness=0.55, metal=0.0):
    m = bpy.data.materials.new(name)
    m.diffuse_color = (*color, 1)
    m.use_nodes = True
    shader = m.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = m.diffuse_color
    shader.inputs["Roughness"].default_value = roughness
    shader.inputs["Metallic"].default_value = metal
    return m


def box(name, position, dimensions, mat, bevel):
    bpy.ops.mesh.primitive_cube_add(size=1, location=position)
    obj = bpy.context.object
    obj.name = name
    obj.scale = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(mat)
    if bevel:
        beveler = obj.modifiers.new("Rounded edges", "BEVEL")
        beveler.width = bevel
        beveler.segments = 3
        obj.modifiers.new("Surface normals", "WEIGHTED_NORMAL")
    return obj


def cylinder(name, position, radius, depth, mat):
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=64, radius=radius, depth=depth, location=position,
        rotation=(math.pi / 2, 0, 0))
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(mat)
    for face in obj.data.polygons:
        face.use_smooth = len(face.vertices) == 4


def aim(obj, point):
    obj.rotation_euler = (Vector(point) - obj.location).to_track_quat("-Z", "Y").to_euler()


def render(case, shot, spec):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    color, label, width = spec
    host = material("Powder-coated enclosure", color)
    matte = material("Black molded plastic", (0.016, 0.019, 0.022))
    metal = material("Slate hardware", (0.14, 0.16, 0.17), 0.4, 0.55)
    blue = material("Matte blue adhesive decal", (0.016, 0.10, 0.60), 0.72)
    ink = material("Printed marking", (0.48, 0.55, 0.57))
    light = material("Laminate tabletop", (0.76, 0.74, 0.70), 0.67)
    both = shot == "S2"
    x = -0.75 if both else 0.0
    box("Tabletop", (0, 0, -0.12), (200, 200, 0.20), light, 0)
    box("Main enclosure", (x, 0, 0.56), (width, 1.26, 1.10), host, 0.09)
    dx = x - 0.55
    cylinder("Dial trim", (dx, -0.64, 0.59), 0.37, 0.065, metal)
    cylinder("Dial", (dx, -0.73, 0.59), 0.335, 0.17, matte)
    box("Dial indicator", (dx + 0.04, -0.817, 0.81), (0.034, 0.015, 0.11), ink, 0.005)
    bpy.ops.object.text_add(location=(x - width / 2 + 0.23, -0.637, 0.98),
                            rotation=(math.pi / 2, 0, 0))
    title = bpy.context.object
    title.data.body = label
    title.data.size = 0.105
    title.data.materials.append(ink)
    for i in range(3):
        cylinder("Inset lamp", (x + 0.74 + i * 0.17, -0.638, 0.23), 0.045, 0.02, metal)
    if both:
        box("Separate accessory", (1.85, -0.12, 0.44), (1.10, 1.02, 0.85), host, 0.065)
    mx, my = (1.85, -0.638) if both else (x + 0.57, -0.636)
    mz = 0.54
    decal = bpy.data.meshes.new("Blue triangle")
    decal.from_pydata([(mx-0.22, my, mz-0.14), (mx+0.22, my, mz-0.14),
                       (mx, my, mz+0.24)], [], [(0, 1, 2)])
    marker = bpy.data.objects.new("Adhesive triangle", decal)
    bpy.context.collection.objects.link(marker)
    decal.materials.append(blue)
    scene.world = bpy.data.worlds.new("Studio ambient")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs[0].default_value = (0.63, 0.69, 0.75, 1)
    scene.world.node_tree.nodes["Background"].inputs[1].default_value = 0.32
    for position, energy, size in [((-3, -4, 6), 800, 5), ((4, -2, 4), 430, 4),
                                   ((0, 3, 5), 500, 4)]:
        bpy.ops.object.light_add(type="AREA", location=position)
        lamp = bpy.context.object
        lamp.data.energy = energy
        lamp.data.shape = "DISK"
        lamp.data.size = size
        aim(lamp, (0, 0, 0))
    azimuth = {"S1": 5, "S2": 13, "S3": 33}[shot]
    angle = math.radians(azimuth)
    bpy.ops.object.camera_add(location=(8.5*math.sin(angle), -8.5*math.cos(angle), 3.1))
    scene.camera = bpy.context.object
    aim(scene.camera, (0, 0, 0.55))
    scene.camera.data.type = "ORTHO"
    scene.camera.data.ortho_scale = 6.45 if both else 5.2
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 24
    scene.cycles.use_denoising = False
    scene.render.resolution_x = 864
    scene.render.resolution_y = 480
    scene.render.resolution_percentage = 100
    scene.render.threads_mode = "FIXED"
    scene.render.threads = 6
    scene.view_settings.view_transform = "Standard"
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.filepath = str(OUT / case / f"{shot}.png")
    bpy.ops.render.render(write_still=True)


for name, values in CASES.items():
    (OUT / name).mkdir(parents=True, exist_ok=True)
    for shot in ("S1", "S2", "S3"):
        render(name, shot, values)
