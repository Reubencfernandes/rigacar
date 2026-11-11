# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 3
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

# <pep8 compliant>

import bpy
import bpy_extras.anim_utils
import mathutils
import math
import itertools
import re

from . import physics


def cursor(cursor_mode):
    def cursor_decorator(func):
        def wrapper(self, context, *args, **kwargs):
            # Scripts and render farms bake without a window, so without a cursor to change.
            window = context.window
            if window is not None:
                window.cursor_modal_set(cursor_mode)
            try:
                return func(self, context, *args, **kwargs)
            finally:
                if window is not None:
                    window.cursor_modal_restore()

        return wrapper

    return cursor_decorator


def bone_name(prefix, position, side, index=0):
    if index == 0:
        return '%s.%s.%s' % (prefix, position, side)
    else:
        return '%s.%s.%s.%03d' % (prefix, position, side, index)


def bone_range(bones, name_prefix, position, side):
    for index in itertools.count():
        name = bone_name(name_prefix, position, side, index)
        if name in bones:
            yield bones[name]
        else:
            break


def find_wheelbrake_bone(bones, position, side, index):
    other_side = 'R' if side == 'L' else 'L'
    name_prefix = 'WheelBrake'
    bone = bones.get(bone_name(name_prefix, position, side, index))
    if bone:
        return bone
    bone = bones.get(bone_name(name_prefix, position, other_side, index))
    if bone:
        return bone
    if index > 0:
        bone = bones.get(bone_name(name_prefix, position, side))
        if bone:
            return bone
        bone = bones.get(bone_name(name_prefix, position, other_side))
        if bone:
            return bone
    backward_compatible_bone_name = '%s Wheels' % ('Front' if position == 'Ft' else 'Back')
    return bones.get(backward_compatible_bone_name)


def _selectable_bone(obj, bone):
    """Blender 5 keeps bone selection on the pose bone; earlier versions keep it on the bone."""
    pose_bone = obj.pose.bones[bone.name]
    return pose_bone if hasattr(pose_bone, 'select') else bone


def is_bone_selected(obj, bone):
    return _selectable_bone(obj, bone).select


def set_bone_selected(obj, bone, selected):
    _selectable_bone(obj, bone).select = selected


def action_fcurves(action, slot=None):
    """F-curves of an action. Slotted actions (Blender 4.4+) keep them per slot; defaults to the first slot."""
    if not hasattr(action, 'slots'):
        return action.fcurves
    if slot is None:
        slot = action.slots[0] if action.slots else None
    if slot is None:
        return None
    channelbag = bpy_extras.anim_utils.action_get_channelbag_for_slot(action, slot)
    return channelbag.fcurves if channelbag is not None else None


def default_bake_frame_range(obj, scene):
    """The frames the rig's own keys span, or the scene's range when it has none (as with an animated path)."""
    animation_data = obj.animation_data
    if animation_data and animation_data.action:
        fcurves = action_fcurves(animation_data.action, getattr(animation_data, 'action_slot', None))
        ranges = [fc.range() for fc in fcurves if len(fc.keyframe_points) > 0] if fcurves is not None else []
        if ranges:
            return math.floor(min(r[0] for r in ranges)), math.ceil(max(r[1] for r in ranges))
    return scene.frame_start, scene.frame_end


def find_fcurves(action, data_path, count):
    fcurves = action_fcurves(action)
    return [fcurves.find(data_path, index=i) if fcurves is not None else None for i in range(count)]


def clear_property_animation(context, property_name, remove_keyframes=True):
    animation_data = context.object.animation_data
    if remove_keyframes and animation_data and animation_data.action:
        fcurve_datapath = '["%s"]' % property_name
        fcurves = action_fcurves(animation_data.action, getattr(animation_data, 'action_slot', None))
        fcurve = fcurves.find(fcurve_datapath) if fcurves is not None else None
        if fcurve is not None:
            fcurves.remove(fcurve)
    context.object[property_name] = .0


def ensure_fcurve(obj, data_path, index=0, group_name='Wheels rotation'):
    """The rig action's F-curve for a channel, created in group_name when missing."""
    action = obj.animation_data.action
    if not hasattr(action, 'slots'):
        fcurve = action.fcurves.find(data_path, index=index)
        return fcurve or action.fcurves.new(data_path, index=index, action_group=group_name)

    fcurve = action.fcurve_ensure_for_datablock(obj, data_path, index=index)
    if fcurve.group is None or fcurve.group.name != group_name:
        channelbag = bpy_extras.anim_utils.action_get_channelbag_for_slot(action, obj.animation_data.action_slot)
        fcurve.group = channelbag.groups.get(group_name) or channelbag.groups.new(group_name)
    return fcurve


def create_property_animation(context, property_name, group_name='Wheels rotation'):
    return ensure_fcurve(context.object, '["%s"]' % property_name, 0, group_name)


def find_rig_fcurve(obj, data_path, index=0):
    animation_data = obj.animation_data
    if not animation_data or not animation_data.action:
        return None
    fcurves = action_fcurves(animation_data.action, getattr(animation_data, 'action_slot', None))
    return fcurves.find(data_path, index=index) if fcurves is not None else None


def remove_fcurve(obj, fcurve):
    animation_data = obj.animation_data
    fcurves = action_fcurves(animation_data.action, getattr(animation_data, 'action_slot', None))
    fcurves.remove(fcurve)


def remove_keyframes(fcurve, frame_start=None, frame_end=None, keyframe_type=None):
    """Removes the keys of an F-curve inside a frame range, optionally only those of one type."""
    points = fcurve.keyframe_points
    for i in range(len(points) - 1, -1, -1):
        point = points[i]
        frame = point.co[0]
        if frame_start is not None and frame < frame_start - .5:
            continue
        if frame_end is not None and frame > frame_end + .5:
            continue
        if keyframe_type is not None and point.type != keyframe_type:
            continue
        points.remove(point, fast=True)
    fcurve.update()


DRIFT_DATA_PATH = 'pose.bones["Drift"].rotation_euler'
DRIFT_INDEX = 2


def clear_baked_drift(obj, frame_start=None, frame_end=None, keyframe_type='JITTER'):
    """Removes baked drift keys (typed Jitter) from the Drift control, and its curve once empty."""
    fcurve = find_rig_fcurve(obj, DRIFT_DATA_PATH, DRIFT_INDEX)
    if fcurve is None:
        return
    remove_keyframes(fcurve, frame_start, frame_end, keyframe_type)
    if len(fcurve.keyframe_points) == 0:
        remove_fcurve(obj, fcurve)
        drift = obj.pose.bones.get('Drift')
        if drift is not None:
            drift.rotation_euler[DRIFT_INDEX] = .0


def write_keyframes(fcurve, points):
    """Inserts baked (frame, value) keys: linear, and typed as Jitter so they read as generated."""
    for frame, value in points:
        kf = fcurve.keyframe_points.insert(frame, value, options={'FAST'})
        kf.interpolation = 'LINEAR'
        kf.type = 'JITTER'
    fcurve.update()


class FCurvesEvaluator(object):
    """Encapsulates a bunch of FCurves for vector animations."""

    def __init__(self, fcurves, default_value):
        self.default_value = default_value
        self.fcurves = fcurves

    def evaluate(self, f):
        result = []
        for fcurve, value in zip(self.fcurves, self.default_value):
            if fcurve is not None:
                result.append(fcurve.evaluate(f))
            else:
                result.append(value)
        return result


class VectorFCurvesEvaluator(object):

    def __init__(self, fcurves_evaluator):
        self.fcurves_evaluator = fcurves_evaluator

    def evaluate(self, f):
        return mathutils.Vector(self.fcurves_evaluator.evaluate(f))


class EulerToQuaternionFCurvesEvaluator(object):

    def __init__(self, fcurves_evaluator):
        self.fcurves_evaluator = fcurves_evaluator

    def evaluate(self, f):
        return mathutils.Euler(self.fcurves_evaluator.evaluate(f)).to_quaternion()


class QuaternionFCurvesEvaluator(object):

    def __init__(self, fcurves_evaluator):
        self.fcurves_evaluator = fcurves_evaluator

    def evaluate(self, f):
        return mathutils.Quaternion(self.fcurves_evaluator.evaluate(f))


def fix_old_steering_rotation(rig_object):
    """
    Fix  armature generated with Rigacar version < 6.0
    """
    if rig_object.pose and rig_object.pose.bones:
        if 'MCH-Steering.rotation' in rig_object.pose.bones:
            rig_object.pose.bones['MCH-Steering.rotation'].rotation_mode = 'QUATERNION'


def serialize_bake_options():
    # For older versions than 4.1
    if bpy.app.version < (4, 1, 0):
        return dict(
            only_selected=True,
            do_pose=True,
            do_object=False,
            do_visual_keying=True
        )
    # For latest versions
    return dict(bake_options=bpy_extras.anim_utils.BakeOptions(
            only_selected=True,
            do_pose=True,
            do_object=False,
            do_visual_keying=True,
            do_constraint_clear=False,
            do_parents_clear=False,
            do_clean=False,
            do_location=True,
            do_scale=True,
            do_rotation=True,
            do_bbone=True,
            do_custom_props=True
        )
    )


class BakingOperator(object):
    frame_start: bpy.props.IntProperty(name='Start Frame', min=1)
    frame_end: bpy.props.IntProperty(name='End Frame', min=1)
    keyframe_tolerance: bpy.props.FloatProperty(name='Keyframe tolerance', min=0, default=.01)

    @classmethod
    def poll(cls, context):
        obj = getattr(context, 'object', None)
        return bool(obj is not None and obj.data is not None and obj.data.get('Car Rig') and
                    obj.mode in ('POSE', 'OBJECT'))

    def invoke(self, context, event):
        self.frame_start, self.frame_end = default_bake_frame_range(context.object, context.scene)
        return context.window_manager.invoke_props_dialog(self)

    def _ensure_action(self, context):
        """Baked keys go into the rig's action, which an animated path leaves the rig without."""
        obj = context.object
        if obj.animation_data is None:
            obj.animation_data_create()
        if obj.animation_data.action is None:
            obj.animation_data.action = bpy.data.actions.new("%sAction" % obj.name)

    def draw(self, context):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        self.layout.prop(self, 'frame_start')
        self.layout.prop(self, 'frame_end')
        self.layout.prop(self, 'keyframe_tolerance')

    def _only_armature_object_moves(self, context):
        """Baking follows the Root bone; reports when only the armature object is animated or constrained."""
        obj = context.object
        animation_data = obj.animation_data
        fcurves = action_fcurves(animation_data.action, getattr(animation_data, 'action_slot', None)) \
            if animation_data and animation_data.action else None
        data_paths = [fc.data_path for fc in fcurves] if fcurves is not None else []
        root = obj.pose.bones.get('Root')
        root_moves = (root is not None and len(root.constraints) > 0) or \
            any(path.startswith('pose.bones["Root"]') for path in data_paths)
        object_moves = len(obj.constraints) > 0 or \
            any(path in ('location', 'rotation_euler', 'rotation_quaternion') or path.startswith('constraints[')
                for path in data_paths)
        if object_moves and not root_moves:
            self.report({'ERROR'}, 'Baking follows the Root bone, but only the armature object moves. '
                                   'Animate the Root bone in Pose mode, or put the Follow Path constraint on it.')
            return True
        return False

    def _create_euler_evaluator(self, action, source_bone):
        fcurve_name = 'pose.bones["%s"].rotation_euler' % source_bone.name
        fc_root_rot = find_fcurves(action, fcurve_name, 3)
        return EulerToQuaternionFCurvesEvaluator(FCurvesEvaluator(fc_root_rot, default_value=(.0, .0, .0)))

    def _create_quaternion_evaluator(self, action, source_bone):
        fcurve_name = 'pose.bones["%s"].rotation_quaternion' % source_bone.name
        fc_root_rot = find_fcurves(action, fcurve_name, 4)
        return QuaternionFCurvesEvaluator(FCurvesEvaluator(fc_root_rot, default_value=(1.0, .0, .0, .0)))

    def _create_location_evaluator(self, action, source_bone):
        fcurve_name = 'pose.bones["%s"].location' % source_bone.name
        fc_root_loc = find_fcurves(action, fcurve_name, 3)
        return VectorFCurvesEvaluator(FCurvesEvaluator(fc_root_loc, default_value=(.0, .0, .0)))

    def _create_scale_evaluator(self, action, source_bone):
        fcurve_name = 'pose.bones["%s"].scale' % source_bone.name
        fc_root_loc = find_fcurves(action, fcurve_name, 3)
        return VectorFCurvesEvaluator(FCurvesEvaluator(fc_root_loc, default_value=(1.0, 1.0, 1.0)))

    def _bake_action(self, context, *source_bones):
        action = context.object.animation_data.action
        nla_tweak_mode = context.object.animation_data.use_tweak_mode if hasattr(context.object.animation_data,
                                                                                 'use_tweak_mode') else False

        # saving context
        obj = context.object
        selected_bones = [b for b in obj.data.bones if is_bone_selected(obj, b)]
        mode = obj.mode
        for b in selected_bones:
            set_bone_selected(obj, b, False)

        bpy.ops.object.mode_set(mode='OBJECT')
        source_bones_matrix_basis = []
        for source_bone in source_bones:
            source_bones_matrix_basis.append(context.object.pose.bones[source_bone.name].matrix_basis.copy())
            set_bone_selected(obj, source_bone, True)

        bake_options = serialize_bake_options()
        baked_action = bpy_extras.anim_utils.bake_action(
            context.object,
            action=None,
            frames=range(self.frame_start, self.frame_end + 1),
            **bake_options
        )

        # restoring context
        for source_bone, matrix_basis in zip(source_bones, source_bones_matrix_basis):
            context.object.pose.bones[source_bone.name].matrix_basis = matrix_basis
            set_bone_selected(obj, source_bone, False)
        for b in selected_bones:
            set_bone_selected(obj, b, True)

        bpy.ops.object.mode_set(mode=mode)

        if nla_tweak_mode:
            context.object.animation_data.use_tweak_mode = nla_tweak_mode
        else:
            context.object.animation_data.action = action

        return baked_action


def frames_per_second(scene):
    return scene.render.fps / (scene.render.fps_base or 1.0)


def metres_per_unit(obj, scene):
    """Metres in one unit of the rig's armature space: the scene's unit scale times the rig's scale."""
    scale = obj.matrix_world.to_scale()
    return (scene.unit_settings.scale_length or 1.0) * (abs(scale.x) + abs(scale.y) + abs(scale.z)) / 3


class WheelsBakeMixin(object):
    """Bakes each Wheel.rotation property from the motion of its wheel, optionally with surface slip."""

    def _wheels(self, context):
        """(wheel rotation bone, brake bone, 'Ft' or 'Bk') for every wheel of the rig."""
        bones = context.object.data.bones
        for position, side in itertools.product(('Ft', 'Bk'), ('L', 'R')):
            for index, wheel_bone in enumerate(bone_range(bones, 'MCH-Wheel.rotation', position, side)):
                yield wheel_bone, find_wheelbrake_bone(bones, position, side, index) or wheel_bone, position

    @cursor('WAIT')
    def _bake_wheels_rotation(self, context, slip=None):
        wheels = list(self._wheels(context))

        for wheel_bone, _, _ in wheels:
            clear_property_animation(context, wheel_bone.name.replace('MCH-', ''))

        bones = set([w[0] for w in wheels] + [w[1] for w in wheels])
        baked_action = self._bake_action(context, *bones)

        if baked_action is None:
            self.report({'WARNING'}, "Existing action failed to bake. Won't bake wheel rotation")
            return

        try:
            for wheel_bone, brake_bone, position in wheels:
                driven = slip is not None and slip.is_driven(position)
                self._bake_wheel_rotation(context, baked_action, wheel_bone, brake_bone, slip, driven)
        finally:
            bpy.data.actions.remove(baked_action)

    def _wheel_motion(self, action, bone, brake_bone, split_sideways=False):
        """Frames from frame_start + 1 to frame_end, with how far the wheel moved since the previous frame.

        Distances are in armature units, signed along the wheel's heading and scaled by its brake.
        Without split_sideways, sliding counts as rolling (Rigacar's classic behaviour); with it, only
        the motion along the heading rolls and the sideways motion is returned apart.
        """
        loc_evaluator = self._create_location_evaluator(action, bone)
        rot_evaluator = self._create_euler_evaluator(action, bone)
        brake_evaluator = self._create_scale_evaluator(action, brake_bone)

        bone_init_vector = (bone.head_local - bone.tail_local).normalized()
        up_vector = mathutils.Vector((0, 0, 1))
        frames, rolling, sideways = [], [], []
        prev_pos = loc_evaluator.evaluate(self.frame_start)
        for f in range(self.frame_start + 1, self.frame_end + 1):
            pos = loc_evaluator.evaluate(f)
            speed_vector = (pos - prev_pos) * (2 * brake_evaluator.evaluate(f).y - 1)
            rotation_quaternion = rot_evaluator.evaluate(f)
            heading = rotation_quaternion @ bone_init_vector
            if split_sideways:
                along = heading.dot(speed_vector)
                side_vector = speed_vector - heading * along
                up = rotation_quaternion @ up_vector
                side_vector -= up * up.dot(side_vector)
                rolling.append(along)
                sideways.append(side_vector.length)
            else:
                rolling.append(math.copysign(speed_vector.magnitude, heading.dot(speed_vector)))
                sideways.append(.0)
            frames.append(f)
            prev_pos = pos
        return frames, rolling, sideways

    def _wheel_keys(self, frames, speeds):
        """Accumulated rotation keyed at each change of speed: linear keys in between stay exact."""
        prev_speed = 0
        distance = 0
        yield self.frame_start, distance
        for f, speed in zip(frames, speeds):
            drop_keyframe = False
            if speed == .0:
                drop_keyframe = prev_speed == speed
            elif prev_speed != .0:
                drop_keyframe = abs(1 - prev_speed / speed) < self.keyframe_tolerance / 10
            if not drop_keyframe:
                prev_speed = speed
                yield f - 1, distance
            distance += speed
        yield self.frame_end, distance

    def _bake_wheel_rotation(self, context, baked_action, bone, brake_bone, slip=None, driven=False):
        fc_rot = create_property_animation(context, bone.name.replace('MCH-', ''))

        # Reset the transform of the wheel bone, otherwise baking yields wrong results
        pb: bpy.types.PoseBone = context.object.pose.bones[bone.name]
        pb.matrix_basis.identity()

        radius = bone.length if bone.length > .0 else 1.0
        frames, rolling, sideways = self._wheel_motion(baked_action, bone, brake_bone, split_sideways=slip is not None)
        if slip is not None:
            # Slip works in metres and seconds; the keys go back in radians per frame.
            fps = frames_per_second(context.scene)
            to_si = fps * metres_per_unit(context.object, context.scene)
            surface = slip.surface_speeds([d * to_si for d in rolling], [d * to_si for d in sideways],
                                          1.0 / fps, driven)
            rolling = [s / to_si for s in surface]

        for f, distance in self._wheel_keys(frames, [d / radius for d in rolling]):
            kf = fc_rot.keyframe_points.insert(f, distance)
            kf.interpolation = 'LINEAR'
            kf.type = 'JITTER'


class ANIM_OT_carWheelsRotationBake(bpy.types.Operator, BakingOperator, WheelsBakeMixin):
    bl_idname = 'anim.car_wheels_rotation_bake'
    bl_label = 'Bake wheels rotation'
    bl_description = 'Automatically generates wheels animation based on Root bone animation.'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if self._only_armature_object_moves(context):
            return {'CANCELLED'}
        self._ensure_action(context)
        context.object['wheels_on_y_axis'] = False
        self._bake_wheels_rotation(context)
        return {'FINISHED'}


class SteeringBakeMixin(object):
    """Bakes Steering.rotation from the path of the front axle. Needs a rotation_factor property."""

    def _bake_steering(self, context):
        bones = context.object.data.bones
        if self.frame_end > self.frame_start and 'Steering' in bones and 'MCH-Steering.rotation' in bones:
            steering = bones['Steering']
            mch_steering_rotation = bones['MCH-Steering.rotation']
            bone_offset = abs(steering.head_local.y - mch_steering_rotation.head_local.y)
            self._bake_steering_rotation(context, bone_offset, mch_steering_rotation)

    def _evaluate_rotation_per_frame(self, action, bone_offset, bone):
        loc_evaluator = self._create_location_evaluator(action, bone)
        rot_evaluator = self._create_quaternion_evaluator(action, bone)

        distance_threshold = pow(bone_offset * max(self.keyframe_tolerance, .001), 2)
        steering_threshold = bone_offset * self.keyframe_tolerance * .1
        bone_direction_vector = (bone.head_local - bone.tail_local).normalized()
        bone_normal_vector = mathutils.Vector((1, 0, 0))

        current_pos = loc_evaluator.evaluate(self.frame_start)
        previous_steering_position = None
        for f in range(self.frame_start, self.frame_end - 1):
            next_pos = loc_evaluator.evaluate(f + 1)
            steering_direction_vector = next_pos - current_pos

            if steering_direction_vector.length_squared < distance_threshold:
                continue

            rotation_quaternion = rot_evaluator.evaluate(f)
            world_space_bone_direction_vector = rotation_quaternion @ bone_direction_vector
            world_space_bone_normal_vector = rotation_quaternion @ bone_normal_vector

            projected_steering_direction = steering_direction_vector.dot(world_space_bone_direction_vector)
            if projected_steering_direction == 0:
                continue

            length_ratio = bone_offset * self.rotation_factor / projected_steering_direction
            steering_direction_vector *= length_ratio

            steering_position = mathutils.geometry.distance_point_to_plane(steering_direction_vector,
                                                                           world_space_bone_direction_vector,
                                                                           world_space_bone_normal_vector)

            if previous_steering_position is not None \
                    and abs(steering_position - previous_steering_position) < steering_threshold:
                continue

            yield f, steering_position
            current_pos = next_pos
            previous_steering_position = steering_position

    def _smoothed_rotation_keys(self, action, bone_offset, bone, smoothing):
        """Steering computed at every frame, averaged over smoothing frames on each side, then thinned to keys.

        A path's heading is rarely as even as it looks: curvature varies between its control points, and the
        steering follows every variation. Averaging keeps the front wheels from twitching.
        """
        loc_evaluator = self._create_location_evaluator(action, bone)
        rot_evaluator = self._create_quaternion_evaluator(action, bone)

        distance_threshold = pow(bone_offset * max(self.keyframe_tolerance, .001), 2)
        bone_direction_vector = (bone.head_local - bone.tail_local).normalized()
        bone_normal_vector = mathutils.Vector((1, 0, 0))

        frames, values = [], []
        steering_position = None
        current_pos = loc_evaluator.evaluate(self.frame_start)
        for f in range(self.frame_start, self.frame_end):
            next_pos = loc_evaluator.evaluate(f + 1)
            steering_direction_vector = next_pos - current_pos
            # A car barely moving has no direction to steer along: it keeps its steering.
            if steering_direction_vector.length_squared >= distance_threshold:
                rotation_quaternion = rot_evaluator.evaluate(f)
                world_space_bone_direction_vector = rotation_quaternion @ bone_direction_vector
                world_space_bone_normal_vector = rotation_quaternion @ bone_normal_vector
                projected_steering_direction = steering_direction_vector.dot(world_space_bone_direction_vector)
                if projected_steering_direction != 0:
                    steering_direction_vector *= bone_offset * self.rotation_factor / projected_steering_direction
                    steering_position = mathutils.geometry.distance_point_to_plane(steering_direction_vector,
                                                                                   world_space_bone_direction_vector,
                                                                                   world_space_bone_normal_vector)
                current_pos = next_pos
            frames.append(f)
            values.append(steering_position)

        first = next((v for v in values if v is not None), None)
        if first is None:
            return []
        values = [first if v is None else v for v in values]
        tolerance = bone_offset * self.keyframe_tolerance * .1
        return physics.reduce_keys(list(zip(frames, physics.smooth(values, smoothing))), tolerance)

    @cursor('WAIT')
    def _bake_steering_rotation(self, context, bone_offset, bone):
        clear_property_animation(context, 'Steering.rotation')
        fix_old_steering_rotation(context.object)
        fc_rot = create_property_animation(context, 'Steering.rotation')

        baked_action = self._bake_action(context, bone)
        if baked_action is None:
            self.report({'WARNING'}, "Existing action failed to bake. Won't bake steering rotation")
            return

        try:
            # Reset the transform of the steering bone, because baking action manipulates the transform
            # and evaluate_rotation_frame expects it at it's default position
            pb: bpy.types.PoseBone = context.object.pose.bones[bone.name]
            pb.matrix_basis.identity()

            smoothing = getattr(self, 'steering_smoothing', 0)
            if smoothing > 0:
                keys = self._smoothed_rotation_keys(baked_action, bone_offset, bone, smoothing)
            else:
                keys = self._evaluate_rotation_per_frame(baked_action, bone_offset, bone)
            for f, steering_pos in keys:
                kf = fc_rot.keyframe_points.insert(f, steering_pos)
                kf.type = 'JITTER'
                kf.interpolation = 'LINEAR'
        finally:
            bpy.data.actions.remove(baked_action)


class ANIM_OT_carSteeringBake(bpy.types.Operator, BakingOperator, SteeringBakeMixin):
    bl_idname = 'anim.car_steering_bake'
    bl_label = 'Bake car steering'
    bl_description = 'Automatically generates steering animation based on Root bone animation.'
    bl_options = {'REGISTER', 'UNDO'}

    rotation_factor: bpy.props.FloatProperty(name='Rotation factor', min=.1, default=1)
    steering_smoothing: bpy.props.IntProperty(name='Smoothing', min=0, max=48, default=0,
                                              description='Frames on each side to average the steering over. '
                                                          'Stops the front wheels twitching on paths with uneven '
                                                          'curvature. 0 keeps the classic bake')

    def draw(self, context):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        self.layout.prop(self, 'frame_start')
        self.layout.prop(self, 'frame_end')
        self.layout.prop(self, 'rotation_factor')
        self.layout.prop(self, 'steering_smoothing')
        self.layout.prop(self, 'keyframe_tolerance')

    def execute(self, context):
        if self._only_armature_object_moves(context):
            return {'CANCELLED'}
        self._ensure_action(context)
        self._bake_steering(context)
        return {'FINISHED'}


class ANIM_OT_carClearSteeringWheelsRotation(bpy.types.Operator):
    bl_idname = "anim.car_clear_steering_wheels_rotation"
    bl_label = "Clear baked animation"
    bl_description = "Clear generated rotation for steering and wheels"
    bl_options = {'REGISTER', 'UNDO'}

    clear_steering: bpy.props.BoolProperty(name="Steering", description="Clear generated animation for steering",
                                           default=True)
    clear_wheels: bpy.props.BoolProperty(name="Wheels", description="Clear generated animation for wheels",
                                         default=True)
    clear_drift: bpy.props.BoolProperty(name="Drift",
                                        description="Clear the drift keys baked by the physics bake. "
                                                    "Drift keys you set by hand are kept",
                                        default=True)
    clear_physics: bpy.props.BoolProperty(name="Suspension and bumps",
                                          description="Clear the baked body pitch, roll and heave and the wheel bumps",
                                          default=True)

    def draw(self, context):
        self.layout.use_property_decorate = False
        self.layout.label(text='Clear generated keyframes for')
        self.layout.prop(self, property='clear_steering')
        self.layout.prop(self, property='clear_wheels')
        self.layout.prop(self, property='clear_drift')
        self.layout.prop(self, property='clear_physics')

    @classmethod
    def poll(cls, context):
        obj = getattr(context, 'object', None)
        return bool(obj is not None and obj.data is not None and obj.data.get('Car Rig'))

    def execute(self, context):
        re_wheel_propname = re.compile(r'^Wheel\.rotation\.(Ft|Bk)\.[LR](\.\d+)?$')
        re_physics_propname = re.compile(r'^(Body\.(pitch|roll|heave)|Wheel\.bump\.(Ft|Bk)\.[LR](\.\d+)?)$')
        for prop in context.object.keys():
            if prop == 'Steering.rotation':
                clear_property_animation(context, prop, remove_keyframes=self.clear_steering)
            elif re_wheel_propname.match(prop):
                clear_property_animation(context, prop, remove_keyframes=self.clear_wheels)
            elif re_physics_propname.match(prop):
                clear_property_animation(context, prop, remove_keyframes=self.clear_physics)
        if self.clear_drift:
            clear_baked_drift(context.object)
        # this is a hack to force Blender to take into account the modification
        # of the properties by changing the object mode.
        # Don't know yet if it is specific to blender 2.80
        mode = context.object.mode
        bpy.ops.object.mode_set(mode='OBJECT' if mode == 'POSE' else 'POSE')
        bpy.ops.object.mode_set(mode=mode)
        return {'FINISHED'}


def register():
    bpy.utils.register_class(ANIM_OT_carWheelsRotationBake)
    bpy.utils.register_class(ANIM_OT_carSteeringBake)
    bpy.utils.register_class(ANIM_OT_carClearSteeringWheelsRotation)


def unregister():
    bpy.utils.unregister_class(ANIM_OT_carClearSteeringWheelsRotation)
    bpy.utils.unregister_class(ANIM_OT_carSteeringBake)
    bpy.utils.unregister_class(ANIM_OT_carWheelsRotationBake)


if __name__ == "__main__":
    register()
