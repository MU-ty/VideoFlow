"""Render predeclared observation holdouts with Blender and FFmpeg.

Usage: blender -b -t 4 --python render_observation_holdout.py -- out.zip
"""
import json
import math
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

import bpy
from mathutils import Vector


STORIES = {
    'cafe_steam_service': dict(
        housing='front of the espresso machine', part='round black steam-control knob',
        target_shape='plus', conflicting_shape='hexagon',
    ),
    'rov_recovery': dict(
        housing='front of the yellow underwater robot', part='silver front carry handle',
        target_shape='hexagon', conflicting_shape='triangle',
    ),
}
SCENARIOS = (
    ('clear-target', 'match', ('target',) * 4),
    ('clear-distractor', 'distinct', ('conflict',) * 4),
    ('target-in-last-panel', 'match', ('conflict',) * 3 + ('target',)),
    ('marker-occluded', 'unknown', ('marker-covered',) * 4),
    ('part-occluded', 'unknown', ('part-covered',) * 4),
    ('marker-on-other-object', 'unknown', ('unbound',) * 4),
)
VIEWS = ((.55, -7.0, 2.95), (-.8, -6.6, 2.60), (.95, -6.5, 2.75), (-.35, -6.7, 2.85))
SHAPE_TEXT = {'plus': 'plus-shaped', 'hexagon': 'hexagonal', 'triangle': 'triangular'}


def material(name, color):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1)
    return mat


def mesh_finish(obj, mat, bevel):
    obj.data.materials.append(mat)
    if bevel:
        mod = obj.modifiers.new('Rounded edges', 'BEVEL')
        mod.width = bevel
        mod.segments = 3
        obj.modifiers.new('Normals', 'WEIGHTED_NORMAL')
    return obj


def box(loc, dims, mat, bevel=.03):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    obj = bpy.context.object
    obj.dimensions = dims
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return mesh_finish(obj, mat, bevel)


def disk(loc, radius, depth, mat, sides=48):
    bpy.ops.mesh.primitive_cylinder_add(vertices=sides, radius=radius, depth=depth,
                                       location=loc, rotation=(math.pi / 2, 0, 0))
    return mesh_finish(bpy.context.object, mat, .005)


def badge(x, y, z, shape, mat):
    if shape == 'plus':
        box((x, y, z), (.53, .055, .15), mat, .014)
        box((x, y, z), (.15, .055, .53), mat, .014)
    else:
        disk((x, y, z), .31, .055, mat, 3 if shape == 'triangle' else 6)


def cafe(materials):
    red, dark, steel, _, _ = materials
    box((0, 0, .13), (10, 7, .24), dark)
    box((0, 1.65, 2.4), (9, .2, 5), steel)
    box((0, 0, 1.47), (3.7, 1.25, 2.45), red, .13)
    box((0, -.71, .53), (3.49, .49, .23), steel)
    box((-.60, -.67, 1.20), (1.26, .16, 1.20), dark, .07)
    box((-.60, -.94, 1.70), (1.30, .48, .20), steel)
    box((-.60, -1.06, 1.48), (.18, .15, .24), steel)
    bpy.ops.mesh.primitive_cone_add(vertices=32, radius1=.24, radius2=.30, depth=.37,
                                    location=(-.60, -1.02, .86))
    mesh_finish(bpy.context.object, steel, .024)
    disk((.91, -.66, 1.50), .39, .18, steel)
    disk((.91, -.80, 1.50), .31, .25, dark, 16)
    box((.91, -.94, 1.53), (.13, .035, .39), dark)
    return (.91, -.66, 2.23), (.91, -1.05, 1.50)


def rov(materials):
    _, dark, steel, yellow, blue = materials
    box((0, 0, .04), (12, 10, .12), steel)
    box((0, 2.2, 2.6), (11, .18, 5.5), blue)
    box((0, 0, 1.45), (3.70, 1.38, 2.15), yellow, .14)
    for x in (-1.58, 1.58):
        box((x, -.04, .37), (.29, 1.58, .29), dark)
        disk((x, -.79, 1.42), .32, .26, dark)
    for x in (-.85, -.35):
        disk((x, -.74, 1.54), .21, .17, dark)
        disk((x, -.84, 1.54), .12, .06, blue)
    for x in (.32, 1.24):
        box((x, -.91, 1.57), (.16, .44, .16), steel, .05)
    box((.78, -1.12, 1.57), (1.05, .16, .16), steel, .05)
    return (.78, -.72, 2.14), (.78, -1.22, 1.57)


def configure():
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.render.resolution_x, scene.render.resolution_y = 480, 270
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.view_settings.view_transform = 'Standard'
    shade = scene.display.shading
    shade.light = 'STUDIO'
    shade.color_type = 'MATERIAL'
    shade.show_shadows = True
    shade.show_cavity = True
    shade.cavity_type = 'BOTH'
    return scene


def render_frame(scene, story, rules, kind, panel, path, materials):
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    _, dark, steel, _, _ = materials
    marker, part = (cafe if story == 'cafe_steam_service' else rov)(materials)
    shape = rules['target_shape'] if kind == 'target' else rules['conflicting_shape']
    if kind == 'unbound':
        # Put the conflicting marker on a separate standing plate next to the device.
        box((-2.62, -.45, 1.20), (.89, .15, 1.5), steel)
        badge(-2.62, -.54, 1.50, shape, dark)
    else:
        badge(*marker, shape, material('ivory badge', (.96, .89, .73)))
    if kind == 'marker-covered':
        box((marker[0], marker[1] - .12, marker[2]), (.82, .16, .69), dark, .015)
    if kind == 'part-covered':
        box((part[0], part[1] - .12, part[2]), (1.42, .15, .88), dark, .015)
    bpy.ops.object.camera_add(location=VIEWS[panel])
    camera = bpy.context.object
    camera.data.lens = 42 if kind == 'unbound' else 45
    aim = Vector((-.60 if kind == 'unbound' else -.30, -.30, 1.48))
    camera.rotation_euler = (aim - camera.location).to_track_quat('-Z', 'Y').to_euler()
    scene.camera = camera
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)


def make_sheet(frames, path):
    command = ['ffmpeg', '-nostdin', '-v', 'error']
    for frame in frames:
        command.extend(('-i', str(frame)))
    subprocess.run(command + [
        '-filter_complex_threads', '1', '-filter_complex',
        '[0:v][1:v][2:v][3:v]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0',
        '-frames:v', '1', '-threads', '1', '-q:v', '3', '-update', '1', str(path),
    ], check=True)


def main():
    output = Path(sys.argv[sys.argv.index('--') + 1]).resolve()
    scene = configure()
    materials = (
        material('red', (.56, .07, .045)), material('black', (.055, .06, .065)),
        material('steel', (.52, .57, .63)), material('yellow', (.92, .67, .055)),
        material('deep blue', (.06, .20, .31)),
    )
    cases = []
    with TemporaryDirectory() as tmp, ZipFile(output, 'x', compression=ZIP_DEFLATED) as bundle:
        root = Path(tmp)
        for story, rules in STORIES.items():
            question = (
                f"In any single panel, is the {rules['part']} mounted on the device with a "
                f"{SHAPE_TEXT[rules['target_shape']]} marker on its own housing? A "
                f"{SHAPE_TEXT[rules['conflicting_shape']]} marker identifies a different device."
            )
            rendered = {}
            for scenario, gold, plan in SCENARIOS:
                frames = []
                for panel, kind in enumerate(plan):
                    key = kind, panel
                    if key not in rendered:
                        frame = root / f'{story}-{kind}-{panel}.png'
                        render_frame(scene, story, rules, kind, panel, frame, materials)
                        rendered[key] = frame
                    frames.append(rendered[key])
                case_id = f'{story}-{scenario}'
                image = f'{case_id}.jpg'
                make_sheet(frames, root / image)
                data = (root / image).read_bytes()
                bundle.writestr(image, data)
                cases.append(dict(id=case_id, group=story, gold=gold, image=image, panels=4,
                                  question=question, planned_panels=plan,
                                  image_sha256=sha256(data).hexdigest()))
        bundle.writestr('protocol.json', json.dumps(dict(
            base_commit='5d1543359785e51751b2ac8a963ae6671860040f',
            observation_rules=STORIES, cases=cases,
        ), indent=2) + '\n')


if __name__ == '__main__':
    main()
