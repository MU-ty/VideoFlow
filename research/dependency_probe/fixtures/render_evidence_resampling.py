"""Render the six holdout source clips with Blender 4.3 in background mode."""
import math
from pathlib import Path
import sys

import bpy
from mathutils import Vector

out = Path(sys.argv[sys.argv.index('--') + 1])
scene = bpy.context.scene
scene.render.engine = 'BLENDER_WORKBENCH'
scene.render.resolution_x, scene.render.resolution_y = 480, 270
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.render.fps = 12
scene.frame_start, scene.frame_end = 1, 48
scene.view_settings.view_transform = 'Standard'
shade = scene.display.shading
shade.light = 'STUDIO'
shade.color_type = 'MATERIAL'
shade.show_shadows = True
shade.show_cavity = True
shade.cavity_type = 'BOTH'


def material(name, rgb):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*rgb, 1)
    return mat


navy = material('navy', (.04, .14, .25))
mint = material('mint', (.10, .48, .32))
ivory = material('ivory', (.9, .87, .70))
red = material('red button', (.8, .026, .035))
yellow = material('yellow indicator', (.92, .63, .02))
black = material('black', (.03, .036, .045))
steel = material('steel', (.36, .43, .52))
concrete = material('concrete', (.31, .29, .25))
sky = material('sky blue', (.22, .42, .63))
grass = material('grass', (.15, .28, .13))


def finish(obj, mat, bevel=.025):
    obj.data.materials.append(mat)
    if bevel:
        mod = obj.modifiers.new('Rounded edges', 'BEVEL')
        mod.width = bevel
        mod.segments = 3
        obj.modifiers.new('Normals', 'WEIGHTED_NORMAL')
    return obj


def box(loc, size, mat, bevel=.025):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    obj = bpy.context.object
    obj.dimensions = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return finish(obj, mat, bevel)


def disk(loc, radius, depth, mat, vertices=48):
    bpy.ops.mesh.primitive_cylinder_add(vertices=vertices, radius=radius, depth=depth,
                                       location=loc, rotation=(math.pi / 2, 0, 0))
    return finish(bpy.context.object, mat, .006)


def station(decoy):
    box((0, .20, 1.4), (9, .22, 5), concrete)
    for z in (.18, .68, 1.18, 1.68, 2.18, 2.68):
        box((0, .076, z), (9, .018, .02), steel, 0)
    box((0, -.13, 1.42), (1.46, .34, 1.67), navy, .09)
    box((.57, -.31, 1.42), (.10, .03, 1.45), steel)
    for z in (1.61, 1.68, 1.75, 1.82, 1.89):
        box((-.33, -.309, z), (.29, .035, .025), black, 0)
    if decoy:
        box((.24, -.325, 1.88), (.38, .03, .08), ivory)
        box((.24, -.325, 1.88), (.08, .03, .38), ivory)
    else:
        disk((.24, -.324, 1.88), .23, .035, ivory, 3)
    disk((.13, -.34, 1.10), .29, .08, black)
    disk((.13, -.405, 1.10), .222, .105, red)


def weather(decoy):
    box((0, 0, -.11), (40, 40, .20), grass, 0)
    box((0, 4.1, 2.5), (24, .13, 8), sky, 0)
    box((0, .19, 1.24), (.13, .13, 2.5), steel)
    box((0, 0, 1.6), (1.48, .42, 1.27), mint, .09)
    box((0, 0, 2.31), (1.66, .56, .19), ivory, .045)
    for x in (-.59, .59):
        box((x, -.23, 1.55), (.04, .035, .74), steel, 0)
    for x in ((-.25, .25) if decoy else (-.28, 0, .28)):
        disk((x, -.225, 1.90), .081, .028, black)
    box((.0, -.234, 1.44), (.69, .06, .30), black, .04)
    box((.0, -.274, 1.44), (.59, .045, .21), yellow, .025)


for name, build in (('station_intercom', station), ('weather_beacon', weather)):
    for i, (position, lens) in enumerate((((.75, -5.5, 2.30), 48),
                                           ((-1.53, -4.75, 2.05), 54),
                                           ((.55, -5.1, 2.22), 50)), 1):
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        build(i == 3)
        bpy.ops.object.camera_add()
        camera = bpy.context.object
        camera.data.lens = lens
        scene.camera = camera
        aim = Vector((0, -.23, 1.54 if name == 'station_intercom' else 1.71))
        for frame, shift in ((1, -.09), (48, .11)):
            camera.location = Vector(position) + Vector((shift, 0, shift * .25))
            camera.rotation_euler = (aim - camera.location).to_track_quat('-Z', 'Y').to_euler()
            camera.keyframe_insert(data_path='location', frame=frame)
            camera.keyframe_insert(data_path='rotation_euler', frame=frame)
        shot_dir = out / name / f'S{i}'
        shot_dir.mkdir(parents=True, exist_ok=True)
        scene.render.filepath = str(shot_dir / 'frame-')
        bpy.ops.render.render(animation=True)
