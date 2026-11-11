"""End-to-end check of the add-on inside Blender.

Operators only run their invoke step with a window, so run Blender with a UI,
under a virtual display when headless, from the repository root:
    xvfb-run -a blender --factory-startup --python tests/e2e.py
Exits non-zero when any check fails.
"""
import json
import math
import os
import sys
import tempfile
import traceback

import bpy
import mathutils

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_NAME = 'rigacar'

failures = []
# The generated suspension settles the body by about a millimetre at rest.
SETTLE = 2e-3


def check(condition, message):
    status = 'PASS' if condition else 'FAIL'
    print(f'[e2e] {status} {message}', flush=True)
    if not condition:
        failures.append(message)


def link_directory(target, link):
    """Symlinks a directory; falls back to a junction on Windows, which needs no developer mode."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != 'nt':
            raise
        import _winapi
        _winapi.CreateJunction(target, link)


def import_addon():
    link_dir = tempfile.mkdtemp(prefix='rigacar-e2e-')
    link_directory(REPO, os.path.join(link_dir, ADDON_NAME))
    sys.path.insert(0, link_dir)
    import rigacar
    rigacar.register()
    return rigacar


def reset_scene():
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for collection in (bpy.data.objects, bpy.data.collections, bpy.data.armatures, bpy.data.meshes,
                       bpy.data.actions, bpy.data.curves):
        for datablock in list(collection):
            collection.remove(datablock)
    view_layer = bpy.context.view_layer
    view_layer.active_layer_collection = view_layer.layer_collection
    bpy.context.scene.cursor.location = (0, 0, 0)


def add_cube(name, location, scale=(1, 1, 1)):
    bpy.ops.mesh.primitive_cube_add(location=location, scale=scale)
    obj = bpy.context.object
    obj.name = name
    return obj


def add_wheel(name, location, scale=1.0):
    bpy.ops.mesh.primitive_cylinder_add(radius=.35 / scale, depth=.25 / scale, location=location,
                                        rotation=(0, 1.5708, 0))
    obj = bpy.context.object
    obj.name = name
    obj.scale = (scale, scale, scale)
    return obj


KENNEY_NAMES = {'body': 'Body', 'wheel-front-left': 'Wheel.Ft.L', 'wheel-front-right': 'Wheel.Ft.R',
                'wheel-back-left': 'Wheel.Bk.L', 'wheel-back-right': 'Wheel.Bk.R'}


def import_sedan():
    """Kenney's CC0 sports sedan (tests/fixtures), with its parts renamed for Rigacar."""
    bpy.ops.import_scene.gltf(filepath=os.path.join(REPO, 'tests', 'fixtures', 'sedan-sports.glb'))
    for old, new in KENNEY_NAMES.items():
        bpy.data.objects[old].name = new
    bpy.context.view_layer.update()
    body = bpy.data.objects['Body']
    wheels = [bpy.data.objects[name] for name in ('Wheel.Ft.L', 'Wheel.Ft.R', 'Wheel.Bk.L', 'Wheel.Bk.R')]
    return body, wheels


def bounds_centre(obj):
    xyz = sys.modules[ADDON_NAME].car_rig.object_bounds_xyz(obj)
    return mathutils.Vector(((xyz[0] + xyz[1]) / 2, (xyz[2] + xyz[3]) / 2, (xyz[4] + xyz[5]) / 2))


def parent_keeping_transform(child, parent):
    world = child.matrix_world.copy()
    child.parent = parent
    child.matrix_world = world


def build_scaled_parented_car():
    """The same world geometry as build_car, from scaled wheels inside a moved, scaled parent."""
    group = bpy.data.objects.new('CarGroup', None)
    bpy.context.scene.collection.objects.link(group)
    group.location = (3, -2, .5)
    group.scale = (.5, .5, .5)
    bpy.context.view_layer.update()
    body, roof, wheels, brake = build_car(wheel_scale=2.0)
    for obj in (body, *wheels, brake):
        parent_keeping_transform(obj, group)
    bpy.context.view_layer.update()
    return body, roof, wheels, brake


def build_car(wheel_scale=1.0):
    body = add_cube('Body', (0, 0, .9), scale=(.9, 2.2, .45))
    # A transformed child must not be transformed twice when sizing the body.
    roof = add_cube('Roof', (0, .3, 1.6), scale=(.7, 1, .2))
    roof.parent = body
    roof.matrix_parent_inverse = body.matrix_world.inverted()
    wheels = [
        add_wheel('Wheel.Ft.L', (.85, -1.4, .35), wheel_scale),
        add_wheel('Wheel.Ft.R', (-.85, -1.4, .35), wheel_scale),
        add_wheel('Wheel.Bk.L', (.85, 1.4, .35), wheel_scale),
        add_wheel('Wheel.Bk.R', (-.85, 1.4, .35), wheel_scale),
    ]
    brake = add_cube('WheelBrake.Ft.L', (.7, -1.4, .35), scale=(.05, .2, .2))
    bpy.context.view_layer.update()
    return body, roof, wheels, brake


def select_pose_bone(rig, name):
    pose_bone = rig.pose.bones[name]
    if hasattr(pose_bone, 'select'):
        pose_bone.select = True
    else:
        rig.data.bones[name].select = True


def viewport():
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
    region = next(r for r in area.regions if r.type == 'WINDOW')
    return bpy.context.temp_override(window=window, area=area, region=region)


def invoke_in_viewport(operator):
    return operator('INVOKE_DEFAULT')


def select_only(objs, active):
    bpy.ops.object.select_all(action='DESELECT')
    for obj in objs:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = active


def test_bounds(car_rig):
    reset_scene()
    body, roof, _, _ = build_car()
    xyz = car_rig.object_bounds_xyz(body)
    # Body spans y in [-2.2, 2.2] and z up to 1.35; the roof reaches z = 1.8.
    expected = [-.9, .9, -2.2, 2.2, .45, 1.8]
    check(all(abs(a - b) < 1e-4 for a, b in zip(xyz, expected)),
          f'bounds include a transformed child exactly once: {[round(v, 3) for v in xyz]}')

    coll = bpy.data.collections.new('CarParts')
    part = bpy.data.objects.new('Part', bpy.data.meshes['Cube'] if 'Cube' in bpy.data.meshes else body.data)
    coll.objects.link(part)
    bpy.ops.object.collection_instance_add(collection=coll.name, location=(10, 0, 0))
    instance = bpy.context.object
    bpy.context.view_layer.update()
    ixyz = car_rig.object_bounds_xyz(instance)
    check(ixyz is not None and abs((ixyz[0] + ixyz[1]) / 2 - 10) < 1e-4,
          f'collection instance bounds follow the instancer: {ixyz and [round(v, 3) for v in ixyz]}')

    empty = bpy.data.objects.new('LonelyEmpty', None)
    bpy.context.scene.collection.objects.link(empty)
    check(car_rig.object_bounds_xyz(empty) is None, 'an empty without geometry has no bounds')


def test_deformation_rig_without_invoke():
    reset_scene()
    result = bpy.ops.object.armature_car_deformation_rig('EXEC_DEFAULT')
    check(result == {'FINISHED'}, f'deformation rig executes without invoke: {result}')
    check('DEF-Body' in bpy.context.object.data.bones, 'exec-only rig has DEF-Body')


def test_hidden_collection_still_rigs():
    reset_scene()
    hidden = bpy.data.collections.new('Hidden')
    bpy.context.scene.collection.children.link(hidden)
    layer_coll = bpy.context.view_layer.layer_collection.children['Hidden']
    bpy.context.view_layer.active_layer_collection = layer_coll
    layer_coll.hide_viewport = True
    result = bpy.ops.object.armature_car_deformation_rig('EXEC_DEFAULT')
    check(result == {'FINISHED'} and 'DEF-Body' in bpy.context.object.data.bones,
          f'hidden active collection still rigs: {result}')


def test_edit_mode_failure_is_reported():
    reset_scene()
    # Without a window, entering edit mode fails with RuntimeError.
    with bpy.context.temp_override(window=None, area=None, region=None):
        try:
            result = bpy.ops.object.armature_car_deformation_rig('EXEC_DEFAULT')
            check(result == {'CANCELLED'}, f'edit mode failure cancels: {result}')
        except RuntimeError as error:
            # bpy.ops raises the operator's error report; it must be ours, not a traceback.
            message = str(error)
            check('Cannot edit the new armature' in message and 'Traceback' not in message,
                  f'edit mode failure is reported: {message.splitlines()[0]}')


def test_full_pipeline():
    reset_scene()
    body, roof, wheels, brake = build_car()
    select_only([body, *wheels, brake], body)
    result = invoke_in_viewport(bpy.ops.object.armature_car_deformation_rig)
    check(result == {'FINISHED'}, f'deformation rig from selection: {result}')
    rig = bpy.context.object
    bones = rig.data.bones
    for name in ('DEF-Body', 'DEF-Wheel.Ft.L', 'DEF-Wheel.Bk.R', 'DEF-WheelBrake.Ft.L'):
        check(name in bones, f'{name} created')
    check(body.parent == rig and body.parent_bone == 'DEF-Body', 'body parented to DEF-Body')
    body_bone = bones['DEF-Body']
    check(abs(body_bone.length - 2.2) < 1e-3, f'DEF-Body length is half the body: {body_bone.length:.3f}')

    result = bpy.ops.pose.car_animation_rig_generate()
    check(result == {'FINISHED'}, f'animation rig generated: {result}')
    check(rig.data['Car Rig'], 'armature flagged as generated')

    collections = {c.name: c for c in rig.data.collections}
    expected_visibility = {
        'CarRig_Default_Ctrls': True,
        'CarRig_Custom_Ctrls': False,
        'CarRig_Def_Bone': False,
        'CarRig_MCH_Bone': False,
        'CarRig_MCH_Bone_Ext': False,
    }
    for name, visible in expected_visibility.items():
        check(name in collections and collections[name].is_visible == visible,
              f'bone collection {name} exists, visible={visible}')
    check('Root' in collections['CarRig_Default_Ctrls'].bones, 'Root in default controls')
    check('DEF-Body' in collections['CarRig_Def_Bone'].bones, 'DEF-Body in deformation collection')
    check(rig.pose.bones['Root'].color.palette == 'THEME04', 'Root uses the direction palette')
    check(rig.pose.bones['Wheel.Ft.L'].color.palette == 'THEME03', 'wheel uses the wheel palette')

    brake_bone = bones.get('WheelBrake.Ft.L')
    check(brake_bone is not None, 'WheelBrake.Ft.L control created')
    wheel_colls = {c.name for c in bones['Wheel.Ft.L'].collections}
    brake_colls = {c.name for c in brake_bone.collections} if brake_bone else set()
    check(wheel_colls and wheel_colls <= brake_colls, f'brake shares wheel collections: {brake_colls}')

    missing_brake = bones.get('WheelBrake.Ft.R')
    check(missing_brake is None, 'no WheelBrake.Ft.R before adding missing brakes')
    bpy.ops.object.mode_set(mode='POSE')
    bpy.ops.pose.select_all(action='DESELECT')
    select_pose_bone(rig, 'Wheel.Ft.R')
    result = bpy.ops.pose.car_animation_add_brake_wheel_bones()
    check(result == {'FINISHED'}, f'missing brake bones added: {result}')
    added = rig.data.bones.get('WheelBrake.Ft.R')
    check(added is not None, 'WheelBrake.Ft.R added')
    if added:
        added_colls = {c.name for c in added.collections}
        right_colls = {c.name for c in rig.data.bones['Wheel.Ft.R'].collections}
        check(right_colls <= added_colls, f'added brake shares wheel collections: {added_colls}')
    bpy.ops.object.mode_set(mode='OBJECT')

    drivers = rig.animation_data.drivers if rig.animation_data else []
    scaled = [d for d in drivers if d.modifiers and d.modifiers[0].type == 'GENERATOR'
              and tuple(d.modifiers[0].coefficients)[1] != 1.0]
    check(len(scaled) > 0, f'influence drivers carry generator modifiers: {len(scaled)}')

    root = rig.pose.bones['Root']
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 24
    root.location = (0, 0, 0)
    root.keyframe_insert('location', frame=1)
    root.location = (0, -5, 0)
    root.keyframe_insert('location', frame=12)
    root.location = (4, -10, 0)
    root.keyframe_insert('location', frame=24)

    result = bpy.ops.anim.car_wheels_rotation_bake('EXEC_DEFAULT', frame_start=1, frame_end=24)
    check(result == {'FINISHED'}, f'wheels rotation baked: {result}')
    scene.frame_set(24)
    check(abs(rig.get('Wheel.rotation.Ft.L', 0)) > 1, f'wheel rotated: {rig.get("Wheel.rotation.Ft.L")}')
    bake_operators = sys.modules[ADDON_NAME].bake_operators
    animation_data = rig.animation_data
    fcurves = bake_operators.action_fcurves(animation_data.action, getattr(animation_data, 'action_slot', None))
    wheel_curve = fcurves.find('["Wheel.rotation.Ft.L"]') if fcurves is not None else None
    check(wheel_curve is not None and wheel_curve.group and wheel_curve.group.name == 'Wheels rotation',
          'baked wheel curve sits in the Wheels rotation group of the rig slot')

    result = bpy.ops.anim.car_steering_bake('EXEC_DEFAULT', frame_start=1, frame_end=24)
    check(result == {'FINISHED'}, f'steering baked: {result}')
    steering = [abs(scene.frame_set(f) or rig.get('Steering.rotation', 0)) for f in range(1, 25)]
    check(max(steering) > .01, f'steering follows the turn: max {max(steering):.3f}')

    result = bpy.ops.anim.car_clear_steering_wheels_rotation('EXEC_DEFAULT')
    check(result == {'FINISHED'}, f'steering and wheels cleared: {result}')
    scene.frame_set(24)
    check(rig.get('Wheel.rotation.Ft.L') == 0 and rig.get('Steering.rotation') == 0,
          'cleared rotations stay at rest')


def rig_car(body, wheels, brake):
    select_only([body, *wheels, brake], body)
    invoke_in_viewport(bpy.ops.object.armature_car_deformation_rig)
    rig = bpy.context.object
    bpy.ops.pose.car_animation_rig_generate()
    bpy.context.view_layer.update()
    return rig


def world_bone_layout(rig):
    return {b.name: (rig.matrix_world @ b.head_local, rig.matrix_world @ b.tail_local) for b in rig.data.bones}


def world_bounds(objs):
    car_rig = sys.modules[ADDON_NAME].car_rig
    return [car_rig.object_bounds_xyz(o) for o in objs]


def max_bounds_delta(before, after):
    return max(abs(a - b) for xyz1, xyz2 in zip(before, after) for a, b in zip(xyz1, xyz2))


def test_rig_drives_model():
    reset_scene()
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    layer = bpy.context.view_layer

    root = rig.pose.bones['Root']
    before = body.matrix_world.translation.copy()
    root.location = (0, 1, 0)
    layer.update()
    moved = (body.matrix_world.translation - before).length
    check(abs(moved - 1) < 1e-3, f'moving Root carries the body: {moved:.3f}')
    root.location = (0, 0, 0)

    wheel_before = wheels[0].matrix_world.to_quaternion()
    rig['Wheel.rotation.Ft.L'] = 1.0
    rig.update_tag()
    layer.update()
    spun = wheel_before.rotation_difference(wheels[0].matrix_world.to_quaternion()).angle
    check(abs(spun - 1) < 1e-3, f'wheel rotation property spins the wheel: {spun:.3f} rad')
    rig['Wheel.rotation.Ft.L'] = 0.0

    steering_before = wheels[0].matrix_world.to_quaternion()
    rig.pose.bones['Steering'].location.x = .3
    rig.update_tag()
    layer.update()
    steered = steering_before.rotation_difference(wheels[0].matrix_world.to_quaternion()).angle
    check(steered > .05, f'steering control turns the front wheel: {steered:.3f} rad')


def test_bake_along_follow_path_keeps_keyframes():
    reset_scene()
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.curve.primitive_bezier_circle_add(radius=8, location=(8, 0, 0))
    path = bpy.context.object
    path.select_set(False)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig

    cns = rig.pose.bones['Root'].constraints.new('FOLLOW_PATH')
    cns.target = path
    cns.use_curve_follow = True
    cns.use_fixed_location = True
    cns.forward_axis = 'TRACK_NEGATIVE_Y'
    data_path = 'pose.bones["Root"].constraints["%s"].offset_factor' % cns.name
    for frame, factor in ((1, 0.0), (48, 0.25)):
        cns.offset_factor = factor
        rig.keyframe_insert(data_path, frame=frame)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 48

    def offset_keys():
        bake_operators = sys.modules[ADDON_NAME].bake_operators
        animation_data = rig.animation_data
        fcurves = bake_operators.action_fcurves(animation_data.action, getattr(animation_data, 'action_slot', None))
        fcurve = fcurves.find(data_path) if fcurves is not None else None
        return len(fcurve.keyframe_points) if fcurve else 0

    result = bpy.ops.anim.car_wheels_rotation_bake('EXEC_DEFAULT', frame_start=1, frame_end=48)
    check(result == {'FINISHED'}, f'follow path: wheels baked: {result}')
    check(offset_keys() == 2, f'follow path: offset keyframes survive the wheel bake: {offset_keys()}')
    result = bpy.ops.anim.car_steering_bake('EXEC_DEFAULT', frame_start=1, frame_end=48)
    check(result == {'FINISHED'}, f'follow path: steering baked: {result}')
    check(offset_keys() == 2, f'follow path: offset keyframes survive the steering bake: {offset_keys()}')
    check('Follow Path' in rig.pose.bones['Root'].constraints, 'follow path: constraint kept')

    scene.frame_set(48)
    # A quarter of a circle of radius 8 is about 12.6 units, over wheels of radius .35.
    turned = abs(rig.get('Wheel.rotation.Ft.L', 0))
    check(turned > 30, f'follow path: wheels roll along the path: {turned:.1f} rad')
    steering = [abs(scene.frame_set(f) or rig.get('Steering.rotation', 0)) for f in range(2, 47)]
    check(min(steering) > .01, f'follow path: steering holds the turn: min {min(steering):.3f}')


def test_bake_explains_an_animated_armature_object():
    reset_scene()
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.location = (0, 0, 0)
    rig.keyframe_insert('location', frame=1)
    rig.location = (0, -10, 0)
    rig.keyframe_insert('location', frame=24)
    for operator in (bpy.ops.anim.car_wheels_rotation_bake, bpy.ops.anim.car_steering_bake):
        try:
            result = operator('EXEC_DEFAULT', frame_start=1, frame_end=24)
            check(False, f'{operator.idname_py()} explains an animated armature object: {result}')
        except RuntimeError as error:
            check('Root bone' in str(error), f'{operator.idname_py()} explains: {str(error).splitlines()[0]}')
    animation_data = rig.animation_data
    fcurves = sys.modules[ADDON_NAME].bake_operators.action_fcurves(animation_data.action,
                                                                     getattr(animation_data, 'action_slot', None))
    check(fcurves is not None and fcurves.find('location', index=1) is not None, 'armature keyframes are kept')


def test_default_bake_frame_range():
    reset_scene()
    bake_operators = sys.modules[ADDON_NAME].bake_operators
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 10, 60
    check(bake_operators.default_bake_frame_range(rig, scene) == (10, 60),
          f'no animation: the scene range: {bake_operators.default_bake_frame_range(rig, scene)}')

    rig.animation_data_create()
    rig.animation_data.action = bpy.data.actions.new('Empty')
    check(bake_operators.default_bake_frame_range(rig, scene) == (10, 60),
          f'an action without keys: the scene range: {bake_operators.default_bake_frame_range(rig, scene)}')

    root = rig.pose.bones['Root']
    for frame in (5, 30):
        root.location.y = -frame / 10
        root.keyframe_insert('location', frame=frame)
    check(bake_operators.default_bake_frame_range(rig, scene) == (5, 30),
          f'keyed Root: the action range: {bake_operators.default_bake_frame_range(rig, scene)}')


def test_bake_along_an_animated_path():
    reset_scene()
    bake_operators = sys.modules[ADDON_NAME].bake_operators
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.curve.primitive_bezier_circle_add(radius=8, location=(8, 0, 0))
    path = bpy.context.object
    path.select_set(False)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 48

    # Follow Path's Animate Path keys the curve's evaluation time; the rig itself has no keys.
    cns = rig.pose.bones['Root'].constraints.new('FOLLOW_PATH')
    cns.target = path
    cns.use_curve_follow = True
    cns.forward_axis = 'TRACK_NEGATIVE_Y'
    path.data.use_path = True
    path.data.path_duration = 48
    for frame, time in ((1, 0.0), (48, 12.0)):
        path.data.eval_time = time
        path.data.keyframe_insert('eval_time', frame=frame)

    frame_start, frame_end = bake_operators.default_bake_frame_range(rig, scene)
    check((frame_start, frame_end) == (1, 48), f'animated path: the dialog offers the scene range: {frame_start}-{frame_end}')
    result = bpy.ops.anim.car_wheels_rotation_bake('EXEC_DEFAULT', frame_start=frame_start, frame_end=frame_end)
    check(result == {'FINISHED'}, f'animated path: wheels baked: {result}')
    scene.frame_set(48)
    # A quarter of the way round a circle of radius 8 is about 12.6 units, over wheels of radius .35.
    turned = abs(rig.get('Wheel.rotation.Ft.L', 0))
    check(turned > 30, f'animated path: wheels roll along the path: {turned:.1f} rad')


def test_real_model_rigs_and_rolls():
    reset_scene()
    for image in list(bpy.data.images):
        bpy.data.images.remove(image)
    body, wheels = import_sedan()
    parts = [body, *wheels]
    before = world_bounds(parts)
    select_only(parts, body)
    invoke_in_viewport(bpy.ops.object.armature_car_deformation_rig)
    rig = bpy.context.object
    check(all(o.parent == rig for o in parts), 'real model: every part follows the rig')
    check(bpy.ops.pose.car_animation_rig_generate() == {'FINISHED'}, 'real model: animation rig generated')
    layer = bpy.context.view_layer
    layer.update()
    check(max_bounds_delta(before, world_bounds(parts)) < SETTLE, 'real model: stays in place when rigged')

    # The wheels' origins sit inboard of their centres; each must still spin about its own centre.
    centre = bounds_centre(wheels[2])
    orientation = wheels[2].matrix_world.to_quaternion()
    rig['Wheel.rotation.Bk.L'] = 1.0
    rig.update_tag()
    layer.update()
    spun = orientation.rotation_difference(wheels[2].matrix_world.to_quaternion()).angle
    drift = (bounds_centre(wheels[2]) - centre).length
    check(abs(spun - 1) < 1e-3 and drift < 1e-3, f'real model: a wheel spins in place: {spun:.3f} rad, centre moved {drift:.4f}')
    rig['Wheel.rotation.Bk.L'] = 0.0

    root = rig.pose.bones['Root']
    start = body.matrix_world.translation.copy()
    root.location = (0, 1, 0)
    layer.update()
    check(abs((body.matrix_world.translation - start).length - 1) < 1e-3, 'real model: moving Root carries the body')


def test_polls_without_a_rig():
    reset_scene()
    addon = sys.modules[ADDON_NAME]
    # Blender swallows an exception raised by poll, so the classes are polled directly.
    operators = (addon.bake_operators.ANIM_OT_carWheelsRotationBake, addon.bake_operators.ANIM_OT_carSteeringBake,
                 addon.bake_operators.ANIM_OT_carClearSteeringWheelsRotation,
                 addon.car_rig.POSE_OT_carAnimationRigGenerate, addon.car_rig.POSE_OT_carAnimationAddBrakeWheelBones)
    empty = bpy.data.objects.new('Empty', None)
    bpy.context.scene.collection.objects.link(empty)
    for active in (None, empty):
        bpy.context.view_layer.objects.active = active
        for operator in operators:
            try:
                check(not operator.poll(bpy.context), f'{operator.bl_idname} is unavailable with {active}')
            except Exception as error:
                check(False, f'{operator.bl_idname} poll raises with {active}: {error!r}')


def test_scaled_parented_model_rigs_like_plain():
    reset_scene()
    body, _, wheels, brake = build_car()
    reference_objects = world_bounds([body, *wheels, brake])
    reference = world_bone_layout(rig_car(body, wheels, brake))
    check(max_bounds_delta(reference_objects, world_bounds([body, *wheels, brake])) < SETTLE,
          'plain model stays in place when rigged')

    reset_scene()
    body, _, wheels, brake = build_scaled_parented_car()
    bpy.context.scene.cursor.location = (1, 2, 0)
    objects_before = world_bounds([body, *wheels, brake])
    layout = world_bone_layout(rig_car(body, wheels, brake))
    # The car has no WheelBrake.Ft.R object, so that brake takes its default place near the 3D cursor.
    deltas = {name: max((layout[name][0] - ref[0]).length, (layout[name][1] - ref[1]).length)
              for name, ref in reference.items() if name in layout and 'WheelBrake.Ft.R' not in name}
    worst = max(deltas, key=deltas.get)
    check(set(layout) == set(reference), f'scaled, parented model, cursor away, makes the same bones: {len(layout)}')
    check(deltas[worst] < 1e-3, f'scaled, parented model makes the same rig: worst {worst} off by {deltas[worst]:.3f}')
    check(max_bounds_delta(objects_before, world_bounds([body, *wheels, brake])) < SETTLE,
          'scaled, parented model stays in place when rigged')
    check(max_bounds_delta(reference_objects, objects_before) < 1e-4, 'both models share the same world geometry')


FORWARD = mathutils.Vector((0, -1, 0))
LEFT = mathutils.Vector((1, 0, 0))


def world_direction(obj, local):
    return (obj.matrix_world.to_3x3() @ local).normalized()


def refresh(rig):
    rig.update_tag()
    bpy.context.view_layer.update()


def drive_along_circle(rig, frames=48, radius=8.0):
    """Root follows a quarter of a circle through the origin over the frames."""
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.curve.primitive_bezier_circle_add(radius=radius, location=(radius, 0, 0))
    path = bpy.context.object
    path.select_set(False)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    cns = rig.pose.bones['Root'].constraints.new('FOLLOW_PATH')
    cns.target = path
    cns.use_curve_follow = True
    cns.use_fixed_location = True
    cns.forward_axis = 'TRACK_NEGATIVE_Y'
    data_path = 'pose.bones["Root"].constraints["%s"].offset_factor' % cns.name
    for frame, factor in ((1, 0.0), (frames, 0.25)):
        cns.offset_factor = factor
        rig.keyframe_insert(data_path, frame=frame)
    for fcurve in bake_fcurves(rig):
        for point in fcurve.keyframe_points:
            point.interpolation = 'LINEAR'
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, frames
    return path


def bake_fcurves(rig):
    animation_data = rig.animation_data
    if not animation_data or not animation_data.action:
        return []
    fcurves = sys.modules[ADDON_NAME].bake_operators.action_fcurves(animation_data.action,
                                                                     getattr(animation_data, 'action_slot', None))
    return list(fcurves) if fcurves is not None else []


def root_positions(rig, frames):
    positions = []
    for frame in frames:
        bpy.context.scene.frame_set(frame)
        positions.append((rig.matrix_world @ rig.pose.bones['Root'].head).copy())
    return positions


def turns_left(rig, frames):
    a, b, c = root_positions(rig, frames)
    return (b - a).cross(c - b).z > 0


def body_yaw_from_root(rig, body, frame):
    """Signed yaw of the body away from the Root's heading, counter-clockwise seen from above: the drift.

    The Root, not its velocity, is the reference: Follow Path adds the curve to the armature object's own
    location, so a rig whose origin is off the path runs a parallel curve and crabs slightly in bends.
    """
    bpy.context.scene.frame_set(frame)
    path_heading = rig.matrix_world.to_3x3() @ rig.pose.bones['Root'].matrix.to_3x3() @ FORWARD
    heading = world_direction(body, FORWARD)
    path_heading.z = heading.z = 0
    return math.atan2(path_heading.cross(heading).z, path_heading.dot(heading))


def physics_bake(**options):
    settings = dict(frame_start=1, frame_end=48)
    settings.update(options)
    return bpy.ops.anim.car_physics_bake('EXEC_DEFAULT', **settings)


def test_physics_channels_drive_the_body():
    reset_scene()
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    for name in ('Body.pitch', 'Body.roll', 'Body.heave', 'Wheel.bump.Ft.L', 'Wheel.bump.Bk.R'):
        check(name in rig.keys(), f'generated rig has the physics channel {name}')

    nose = world_direction(body, FORWARD).z
    rig['Body.pitch'] = .1
    refresh(rig)
    check(world_direction(body, FORWARD).z - nose > .09, f'Body.pitch raises the nose: {world_direction(body, FORWARD).z:.3f}')
    rig['Body.pitch'] = .0

    rig['Body.roll'] = .1
    refresh(rig)
    left = world_direction(body, LEFT).z
    check(left > .09, f'Body.roll leans the body to its right, lifting its left: {left:.3f}')
    rig['Body.roll'] = .0

    height = body.matrix_world.translation.z
    rig['Body.heave'] = .1
    refresh(rig)
    check(abs(body.matrix_world.translation.z - height - .1) < 1e-3, 'Body.heave lifts the body')
    rig['Body.heave'] = .0

    wheel_heights = [w.matrix_world.translation.z for w in wheels]
    brake_height = brake.matrix_world.translation.z
    rig['Wheel.bump.Ft.L'] = .05
    refresh(rig)
    lifted = [w.matrix_world.translation.z - h for w, h in zip(wheels, wheel_heights)]
    check(abs(lifted[0] - .05) < 1e-3 and all(abs(d) < 1e-6 for d in lifted[1:]),
          f'Wheel.bump lifts only its wheel: {[round(d, 3) for d in lifted]}')
    check(abs(brake.matrix_world.translation.z - brake_height - .05) < 1e-3, 'the brake rides with its wheel')
    rig['Wheel.bump.Ft.L'] = .0
    refresh(rig)


def test_set_ground_follows_terrain():
    reset_scene()
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 0, .1))
    ground = bpy.context.object
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    resting = body.matrix_world.translation.z

    result = bpy.ops.object.car_rig_set_ground('EXEC_DEFAULT', ground=ground.name, travel=.3)
    check(result == {'FINISHED'}, f'set ground: {result}')
    sensors = list(sys.modules[ADDON_NAME].car_rig.enumerate_ground_sensors(rig.pose.bones))
    projections = [s.constraints['Ground projection'] for s in sensors]
    check(len(sensors) == 6 and all(c.target == ground and c.shrinkwrap_type == 'PROJECT' for c in projections),
          f'every sensor raycasts onto the ground: {len(sensors)}')
    limits = [s.constraints['Ground projection limitation'] for s in sensors if 'Ground projection limitation' in s.constraints]
    check(limits and all(abs(c.max_z - .3) < 1e-6 and abs(c.min_z + .3) < 1e-6 for c in limits), 'travel sets the limits')
    refresh(rig)
    check(abs(body.matrix_world.translation.z - resting - .1) < 2e-3,
          f'the car rides on the raised ground: {body.matrix_world.translation.z - resting:.3f}')

    # A ramp rising towards the front tilts the car nose up.
    ground.location.z = 0
    ground.rotation_euler.x = math.radians(-6)
    refresh(rig)
    nose = world_direction(body, FORWARD).z
    check(nose > .03, f'the car follows a slope: nose {nose:.3f}')

    result = bpy.ops.object.car_rig_set_ground('EXEC_DEFAULT', ground='')
    check(result == {'FINISHED'} and all(c.target is None for c in projections), 'clearing the ground')
    refresh(rig)
    check(abs(body.matrix_world.translation.z - resting) < 2e-3, 'without ground the car is back on the floor')

    try:
        bpy.ops.object.car_rig_set_ground('EXEC_DEFAULT', ground=body.name)
        check(False, 'the car may not drive on itself')
    except RuntimeError as error:
        check('itself' in str(error), f'the car may not drive on itself: {str(error).splitlines()[0]}')


def test_physics_bake_drifts_on_loose_ground():
    reset_scene()
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    drive_along_circle(rig)
    left = turns_left(rig, (10, 20, 30))

    result = physics_bake(surface='ASPHALT')
    check(result == {'FINISHED'}, f'physics baked on asphalt: {result}')
    yaw = max(abs(body_yaw_from_root(rig, body, f)) for f in range(6, 44, 4))
    check(yaw < math.radians(1), f'asphalt holds a 0.5 g corner without drifting: {math.degrees(yaw):.1f}Â°')
    bpy.context.scene.frame_set(30)
    asphalt_roll = rig.get('Body.roll', 0)
    check((asphalt_roll > 0) == left and abs(asphalt_roll) > math.radians(.5),
          f'the body leans out of the corner: {math.degrees(asphalt_roll):.2f}Â°')

    result = physics_bake(surface='MUD')
    check(result == {'FINISHED'}, f'physics baked on mud: {result}')
    yaws = [body_yaw_from_root(rig, body, f) for f in range(30, 46, 2)]
    worst = max(yaws, key=abs)
    check(abs(worst) > math.radians(8), f'mud lets the rear slide out: {math.degrees(worst):.1f}Â°')
    check((worst > 0) == left, f'the car yaws into the turn, the rear swinging out: left turn {left}, '
                              f'yaw {math.degrees(worst):.1f}Â°')

    bpy.context.scene.frame_set(44)
    steering = rig.get('Steering.rotation', 0)
    check(abs(steering) > .01, f'steering baked with the physics: {steering:.3f}')
    rear, front = abs(rig.get('Wheel.rotation.Bk.L', 0)), abs(rig.get('Wheel.rotation.Ft.L', 0))
    check(rear > front * 1.05, f'the driven rear wheels spin while sliding: rear {rear:.1f}, front {front:.1f}')

    drift_curve = next((fc for fc in bake_fcurves(rig) if fc.data_path == 'pose.bones["Drift"].rotation_euler'), None)
    check(drift_curve is not None and all(p.type == 'JITTER' for p in drift_curve.keyframe_points),
          'drift is keyed on the Drift control as baked keys')

    result = physics_bake(surface='ICE')
    yaws_ice = [body_yaw_from_root(rig, body, f) for f in range(30, 46, 2)]
    check(max(map(abs, yaws_ice)) > abs(worst), f'ice drifts more than mud: {math.degrees(max(map(abs, yaws_ice))):.1f}Â°')


def keyed_straight_run(rig, distance=20.0, frames=48):
    """Root starts and stops: Bezier keys accelerate over the first half and brake over the second."""
    root = rig.pose.bones['Root']
    root.location = (0, 0, 0)
    root.keyframe_insert('location', frame=1)
    root.location = (0, -distance, 0)
    root.keyframe_insert('location', frame=frames)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, frames


def test_physics_bake_pitches_and_slips():
    reset_scene()
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    keyed_straight_run(rig)
    scene = bpy.context.scene

    result = physics_bake(surface='ASPHALT', bake_drift=False)
    check(result == {'FINISHED'}, f'physics baked on a straight run: {result}')
    pitches = {f: (scene.frame_set(f), rig.get('Body.pitch', 0))[1] for f in range(1, 49)}
    accelerating, braking = max(pitches[f] for f in range(4, 20)), min(pitches[f] for f in range(30, 46))
    check(accelerating > math.radians(.5), f'the nose rises when accelerating: {math.degrees(accelerating):.2f}Â°')
    check(braking < -math.radians(.5), f'the nose dives when braking: {math.degrees(braking):.2f}Â°')
    scene.frame_set(48)
    asphalt_rotation = abs(rig.get('Wheel.rotation.Bk.L', 0))
    expected = 20 / .35
    check(abs(asphalt_rotation - expected) / expected < .03,
          f'on asphalt the wheels roll without slip: {asphalt_rotation:.1f} rad of {expected:.1f}')
    check(rig.get('Body.heave', 0) == 0 or abs(rig.get('Body.heave', 0)) < 1e-3, 'smooth asphalt keeps the ride height')

    physics_bake(surface='MUD', bake_drift=False, drive='RWD')
    scene.frame_set(16)
    rear, front = abs(rig.get('Wheel.rotation.Bk.L', 0)), abs(rig.get('Wheel.rotation.Ft.L', 0))
    check(rear > front * 1.1, f'mud: the rear-driven wheels spin on the launch: rear {rear:.1f}, front {front:.1f}')
    physics_bake(surface='MUD', bake_drift=False, drive='FWD')
    scene.frame_set(16)
    rear, front = abs(rig.get('Wheel.rotation.Bk.L', 0)), abs(rig.get('Wheel.rotation.Ft.L', 0))
    check(front > rear * 1.1, f'mud: front-wheel drive spins the front instead: front {front:.1f}, rear {rear:.1f}')

    physics_bake(surface='ICE', bake_drift=False)
    scene.frame_set(48)
    ice_front = abs(rig.get('Wheel.rotation.Ft.L', 0))
    check(ice_front < asphalt_rotation * .9, f'ice: the wheels lock when braking: {ice_front:.1f} rad')


def test_physics_bake_rough_mud_shakes_and_sinks():
    reset_scene()
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    keyed_straight_run(rig)
    scene = bpy.context.scene
    physics_bake(surface='MUD', bake_drift=False)
    bumps = [(scene.frame_set(f), rig.get('Wheel.bump.Ft.L', 0))[1] for f in range(1, 49)]
    heave = [(scene.frame_set(f), rig.get('Body.heave', 0))[1] for f in range(1, 49)]
    spread = max(bumps) - min(bumps)
    check(spread > .01, f'rough mud shakes the wheels: {spread:.3f} spread')
    check(-.1 < sum(bumps) / len(bumps) < -.03,
          f'the tyres sink into the mud: mean {sum(bumps) / len(bumps):.3f}')
    check(sum(heave) / len(heave) < -.03, f'the body sinks with them: mean {sum(heave) / len(heave):.3f}')
    heave_spread = max(heave) - min(heave)
    check(heave_spread < spread, f'the springs soften the shaking for the body: {heave_spread:.3f} < {spread:.3f}')

    # The ground is fixed in the world: a car standing still does not shake.
    front_still = [(scene.frame_set(f), rig.get('Wheel.bump.Ft.L', 0))[1] for f in (1, 2)]
    check(abs(front_still[0] - front_still[1]) < 5e-3, 'a car at rest barely moves on rough ground')


def test_clear_keeps_hand_made_drift():
    reset_scene()
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    drive_along_circle(rig)
    drift = rig.pose.bones['Drift']
    drift.rotation_euler.z = .3
    drift.keyframe_insert('rotation_euler', index=2, frame=80)
    physics_bake(surface='MUD')
    result = bpy.ops.anim.car_clear_steering_wheels_rotation('EXEC_DEFAULT')
    check(result == {'FINISHED'}, f'cleared: {result}')
    curve = next((fc for fc in bake_fcurves(rig) if fc.data_path == 'pose.bones["Drift"].rotation_euler'), None)
    frames = [p.co[0] for p in curve.keyframe_points] if curve else []
    check(frames == [80.0], f'clearing keeps the hand-made drift key only: {frames}')
    physics_paths = [fc.data_path for fc in bake_fcurves(rig)
                     if fc.data_path.startswith('["Body.') or fc.data_path.startswith('["Wheel.bump')]
    check(not physics_paths, f'clearing removes suspension and bumps: {physics_paths}')


def test_old_rig_gains_physics_channels():
    reset_scene()
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    # A rig from Rigacar 8 or older has no physics channels.
    for fcurve in list(rig.animation_data.drivers):
        if fcurve.data_path.startswith('pose.bones["MCH-Body"]') or \
                (fcurve.data_path.startswith('pose.bones["MCH-Wheel') and fcurve.data_path.endswith('location')):
            rig.animation_data.drivers.remove(fcurve)
    for name in [k for k in rig.keys() if k.startswith('Body.') or k.startswith('Wheel.bump')]:
        del rig[name]
    keyed_straight_run(rig)
    result = physics_bake(surface='DIRT', bake_drift=False)
    check(result == {'FINISHED'}, f'an old rig bakes physics: {result}')
    check(all(name in rig.keys() for name in ('Body.pitch', 'Body.roll', 'Body.heave', 'Wheel.bump.Bk.R')),
          'and gains the physics channels')
    drivers = [d.data_path for d in rig.animation_data.drivers if d.data_path.startswith('pose.bones["MCH-Body"]')]
    check(len(drivers) == 3, f'with drivers on the body: {drivers}')


def test_follow_path_keeps_the_car_on_the_curve():
    reset_scene()
    body, _, wheels, brake = build_car()
    rig = rig_car(body, wheels, brake)
    bpy.ops.object.mode_set(mode='OBJECT')
    centre = mathutils.Vector((20, 5, 0))
    bpy.ops.curve.primitive_bezier_circle_add(radius=8, location=centre)
    path = bpy.context.object
    # Curves made by scripts or converted from meshes come without path animation: the helper turns it on.
    path.data.use_path = False
    bpy.context.view_layer.objects.active = rig
    result = bpy.ops.object.car_rig_follow_path('EXEC_DEFAULT', curve=path.name, frame_start=1, frame_end=96,
                                                easing='LINEAR')
    check(result == {'FINISHED'}, f'follow path helper: {result}')
    distances = [(p - centre).length for p in root_positions(rig, (1, 24, 48, 72))]
    check(all(abs(d - 8) < .05 for d in distances),
          f'the Root rides on the curve itself: {[round(d, 3) for d in distances]}')
    crab = 0.0
    for frame in (20, 40, 60):
        before, after = root_positions(rig, (frame - 1, frame + 1))
        velocity = after - before
        bpy.context.scene.frame_set(frame)
        heading = rig.matrix_world.to_3x3() @ rig.pose.bones['Root'].matrix.to_3x3() @ FORWARD
        crab = max(crab, abs(math.atan2(velocity.cross(heading).z, velocity.dot(heading))))
    check(crab < math.radians(1), f'the car faces where it goes, without crabbing: {math.degrees(crab):.2f}°')

    result = bpy.ops.anim.car_wheels_rotation_bake('EXEC_DEFAULT', frame_start=1, frame_end=96)
    bpy.context.scene.frame_set(96)
    # The rear axle runs the curve; its wheels run circles half a track wider and narrower.
    expected = 2 * math.pi * 8 / .35
    left, right = abs(rig.get('Wheel.rotation.Bk.L', 0)), abs(rig.get('Wheel.rotation.Bk.R', 0))
    check(abs((left + right) / 2 - expected) / expected < .03,
          f'the rear wheels roll the lap: {(left + right) / 2:.1f} of {expected:.1f} rad')
    outer, inner = max(left, right), min(left, right)
    check(abs(outer / inner - 8.85 / 7.15) < .03, f'the outer wheel turns more than the inner: {outer / inner:.3f}')

    # A scaled curve only warns: the car still follows it.
    path.scale = (2, 2, 2)
    bpy.context.view_layer.objects.active = rig
    result = bpy.ops.object.car_rig_follow_path('EXEC_DEFAULT', curve=path.name, frame_start=1, frame_end=96)
    check(result == {'FINISHED'}, f'a scaled curve is followed with a warning: {result}')
    check(len([c for c in rig.pose.bones['Root'].constraints if c.type == 'FOLLOW_PATH']) == 1,
          'following again replaces the constraint')


def test_redo_keeps_the_rigged_parts():
    reset_scene()
    body, _, wheels, brake = build_car()
    # Adjust Last Operation undoes and runs execute again with the properties invoke filled in,
    # target_objects among them, while the parts are no longer selected.
    targets = json.dumps({'Body': body.name, 'Wheel.Ft.L': wheels[0].name, 'Wheel.Ft.R': wheels[1].name,
                          'Wheel.Bk.L': wheels[2].name, 'Wheel.Bk.R': wheels[3].name, 'WheelBrake.Ft.L': brake.name})
    bpy.ops.object.select_all(action='DESELECT')
    result = bpy.ops.object.armature_car_deformation_rig('EXEC_DEFAULT', target_objects=targets,
                                                         nb_front_wheel_brakes_pairs=1)
    rig = bpy.context.object
    check(result == {'FINISHED'} and body.parent == rig and all(w.parent == rig for w in wheels),
          'running it again from its properties rigs the same parts')
    check(abs(rig.data.bones['DEF-Wheel.Ft.L'].head_local.y - (-1.4)) < 1e-3, 'at their own places')


def main():
    print(f'[e2e] Blender {bpy.app.version_string}', flush=True)
    rigacar = import_addon()
    for test in (lambda: test_bounds(rigacar.car_rig), test_deformation_rig_without_invoke,
                 test_hidden_collection_still_rigs, test_edit_mode_failure_is_reported,
                 test_full_pipeline, test_rig_drives_model, test_bake_along_follow_path_keeps_keyframes,
                 test_bake_explains_an_animated_armature_object, test_default_bake_frame_range,
                 test_bake_along_an_animated_path, test_real_model_rigs_and_rolls, test_polls_without_a_rig,
                 test_scaled_parented_model_rigs_like_plain, test_physics_channels_drive_the_body,
                 test_set_ground_follows_terrain, test_physics_bake_drifts_on_loose_ground,
                 test_physics_bake_pitches_and_slips, test_physics_bake_rough_mud_shakes_and_sinks,
                 test_clear_keeps_hand_made_drift, test_old_rig_gains_physics_channels,
                 test_redo_keeps_the_rigged_parts, test_follow_path_keeps_the_car_on_the_curve):
        try:
            with viewport():
                test()
        except Exception:
            traceback.print_exc()
            check(False, f'{getattr(test, "__name__", test)} raised; see the traceback above')
    print(f'[e2e] {len(failures)} failure(s)', flush=True)
    os._exit(1 if failures else 0)


if __name__ == '__main__':
    if bpy.app.background:
        print('[e2e] FAIL needs a window: run under xvfb-run without --background', flush=True)
        os._exit(2)
    bpy.app.timers.register(main, first_interval=.5)
