"""Checks the add-on in background mode, as scripts, render farms and CI run it. No window needed:
    blender --background --factory-startup --python tests/background.py
Exits non-zero when any check fails.
"""
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e  # noqa: E402


def main():
    print(f'[e2e] Blender {bpy.app.version_string} (background)', flush=True)
    e2e.import_addon()
    e2e.reset_scene()
    body, _, wheels, brake = e2e.build_car()
    e2e.select_only([body, *wheels, brake], body)
    result = bpy.ops.object.armature_car_deformation_rig('INVOKE_DEFAULT')
    e2e.check(result == {'FINISHED'}, f'background: deformation rig from the selection: {result}')
    rig = bpy.context.object
    e2e.check(body.parent == rig and all(w.parent == rig for w in wheels), 'background: the car follows the rig')
    e2e.check(bpy.ops.pose.car_animation_rig_generate() == {'FINISHED'}, 'background: animation rig generated')
    bpy.ops.object.mode_set(mode='OBJECT')

    e2e.keyed_straight_run(rig)
    for operator, options in ((bpy.ops.anim.car_wheels_rotation_bake, {}),
                              (bpy.ops.anim.car_steering_bake, {}),
                              (bpy.ops.anim.car_physics_bake, {'surface': 'MUD'})):
        result = operator('EXEC_DEFAULT', frame_start=1, frame_end=48, **options)
        e2e.check(result == {'FINISHED'}, f'background: {operator.idname_py()}: {result}')

    scene = bpy.context.scene
    scene.frame_set(24)
    e2e.check(abs(rig.get('Wheel.rotation.Bk.L', 0)) > 1, 'background: the wheels turn')
    e2e.check(abs(rig.get('Wheel.bump.Ft.L', 0)) > 1e-4, 'background: mud lifts and sinks the wheels')

    result = bpy.ops.anim.car_clear_steering_wheels_rotation('EXEC_DEFAULT')
    e2e.check(result == {'FINISHED'}, f'background: baked animation cleared: {result}')
    print(f'[e2e] {len(e2e.failures)} failure(s)', flush=True)
    os._exit(1 if e2e.failures else 0)


main()
