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

"""Operators that put physics on a generated car rig: the physics bake and the ground set-up."""

import math

import bpy
import mathutils

from . import physics
from . import car_rig
from .bake_operators import (BakingOperator, WheelsBakeMixin, SteeringBakeMixin, DRIFT_DATA_PATH, DRIFT_INDEX,
                             clear_baked_drift, clear_property_animation, create_property_animation, cursor,
                             ensure_fcurve, frames_per_second, metres_per_unit, write_keyframes)

WORLD_UP = mathutils.Vector((0, 0, 1))


def is_generated_car_rig(context):
    obj = getattr(context, 'object', None)
    return bool(obj is not None and obj.type == 'ARMATURE' and obj.data is not None and obj.data.get('Car Rig'))


def sample_bones(context, obj, bone_names, frames):
    """World-space (matrix, head, tail) of pose bones at each frame, as evaluated with constraints and drivers."""
    scene = context.scene
    samples = {name: [] for name in bone_names}
    for frame in frames:
        scene.frame_set(frame)
        evaluated = obj.evaluated_get(context.evaluated_depsgraph_get())
        world = evaluated.matrix_world
        for name in bone_names:
            pose_bone = evaluated.pose.bones[name]
            matrix = world @ pose_bone.matrix
            samples[name].append((matrix, matrix.translation.copy(), world @ pose_bone.tail))
    return samples


def smoothed_vector_derivative(points, dt, radius):
    derived = physics.vector_derivative([tuple(p) for p in points], dt)
    columns = [physics.smooth(list(column), radius) for column in zip(*derived)] if derived else [[], [], []]
    return [mathutils.Vector(v) for v in zip(*columns)]


def mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def apply_surface_preset(self, context):
    preset = physics.SURFACES.get(self.surface)
    if preset is None:
        return
    _, grip, roughness, bump_size, sink, _ = preset
    # Presets are in metres; the operator's distances are in scene units.
    unit = context.scene.unit_settings.scale_length or 1.0
    self.grip = grip
    self.roughness = roughness / unit
    self.bump_size = bump_size / unit
    self.sink = sink / unit


SURFACE_ITEMS = [(key, value[0], value[5]) for key, value in physics.SURFACES.items()] + \
                [('CUSTOM', 'Custom', 'Set grip, roughness and sink yourself')]


class ANIM_OT_carPhysicsBake(bpy.types.Operator, BakingOperator, WheelsBakeMixin, SteeringBakeMixin):
    bl_idname = 'anim.car_physics_bake'
    bl_label = 'Bake car physics'
    bl_description = ('Simulates drift, wheel slip, road bumps and a sprung body from the Root bone motion, '
                      'for the chosen surface, and bakes them')
    bl_options = {'REGISTER', 'UNDO'}

    surface: bpy.props.EnumProperty(name='Surface', items=SURFACE_ITEMS, default='ASPHALT',
                                    update=apply_surface_preset,
                                    description='What the car drives on. Sets grip, roughness and sink')
    grip: bpy.props.FloatProperty(name='Grip', min=.02, max=1.5, default=1.0,
                                  description='Friction of the tyres on the surface: 1 on dry asphalt, '
                                              '0.35 in mud, 0.1 on ice')
    roughness: bpy.props.FloatProperty(name='Roughness', min=0, max=1, default=0, subtype='DISTANCE',
                                       description='Height of the bumps of rough ground')
    bump_size: bpy.props.FloatProperty(name='Bump Size', min=.05, max=50, default=2, subtype='DISTANCE',
                                       description='Distance between bumps of rough ground')
    sink: bpy.props.FloatProperty(name='Tyre Sink', min=0, max=1, default=0, subtype='DISTANCE',
                                  description='How deep the tyres sink into soft ground such as mud, sand or snow')
    seed: bpy.props.IntProperty(name='Seed', min=0, default=0, description='Changes the pattern of the bumps')

    bake_drift: bpy.props.BoolProperty(name='Drift', default=True,
                                       description='Slide the rear out in corners taken faster than the grip allows. '
                                                   'Replaces the Drift keys inside the frame range')
    bake_steering: bpy.props.BoolProperty(name='Steering', default=True,
                                          description='Steer the front wheels along the path')
    bake_wheels: bpy.props.BoolProperty(name='Wheels', default=True,
                                        description='Spin the wheels, with wheelspin and lock-up on loose ground')
    bake_suspension: bpy.props.BoolProperty(name='Suspension', default=True,
                                            description='Let the sprung body pitch, roll and bounce, and the wheels '
                                                        'ride over rough ground')

    drift_strength: bpy.props.FloatProperty(name='Strength', min=0, max=5, default=1,
                                            description='How quickly the slide grows once grip runs out')
    drift_max_angle: bpy.props.FloatProperty(name='Max Angle', min=0, max=math.radians(90),
                                             default=math.radians(40), subtype='ANGLE',
                                             description='Largest drift angle')
    drift_onset: bpy.props.FloatProperty(name='Onset', min=.1, max=3, default=.8,
                                         description='Fraction of the grip a corner may use before the rear slides. '
                                                     'Lower drifts sooner')
    rotation_factor: bpy.props.FloatProperty(name='Steering Factor', min=.1, default=1,
                                             description='Scales the baked steering')
    steering_smoothing: bpy.props.IntProperty(name='Smoothing', min=0, max=48, default=2,
                                              description='Frames on each side to average the steering over. '
                                                          'Stops the front wheels twitching on paths with uneven '
                                                          'curvature')

    drive: bpy.props.EnumProperty(name='Drive', default='RWD',
                                  items=[('RWD', 'Rear', 'Rear-wheel drive'),
                                         ('FWD', 'Front', 'Front-wheel drive'),
                                         ('AWD', 'All', 'All-wheel drive')],
                                  description='Which wheels the engine drives: those spin on loose ground')
    wheelspin: bpy.props.FloatProperty(name='Wheelspin', min=0, max=5, default=1,
                                       description='How much driven wheels spin when accelerating on loose ground')
    brake_lock: bpy.props.FloatProperty(name='Brake Lock', min=0, max=5, default=1,
                                        description='How much wheels lock when slowing down on slippery ground')
    drift_spin: bpy.props.FloatProperty(name='Drift Spin', min=0, max=5, default=1,
                                        description='How much driven wheels spin while sliding sideways')

    suspension_frequency: bpy.props.FloatProperty(name='Frequency', min=.3, max=6, default=1.5,
                                                  description='Bounces per second of the body on its springs: '
                                                              'about 1 for a soft saloon, 2.5 for a race car')
    suspension_damping: bpy.props.FloatProperty(name='Damping', min=.05, max=2, default=.45,
                                                description='How quickly a bounce dies out: '
                                                            'low keeps bouncing, 1 settles without overshoot')
    pitch_per_g: bpy.props.FloatProperty(name='Pitch per g', min=0, max=math.radians(20),
                                         default=math.radians(3.5), subtype='ANGLE',
                                         description='How far the nose rises under 1 g of acceleration '
                                                     'and dips under braking')
    roll_per_g: bpy.props.FloatProperty(name='Roll per g', min=0, max=math.radians(20),
                                        default=math.radians(5), subtype='ANGLE',
                                        description='How far the body leans out of a 1 g corner')

    def invoke(self, context, event):
        return BakingOperator.invoke(self, context, event)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        col = layout.column(align=True)
        col.prop(self, 'frame_start')
        col.prop(self, 'frame_end')

        box = layout.box()
        box.label(text='Surface', icon='WORLD')
        box.prop(self, 'surface')
        col = box.column(align=True)
        # A preset decides these; Custom lets them be edited, starting from the last preset.
        col.enabled = self.surface == 'CUSTOM'
        col.prop(self, 'grip')
        col.prop(self, 'roughness')
        col.prop(self, 'bump_size')
        col.prop(self, 'sink')
        box.prop(self, 'seed')

        box = layout.box()
        box.prop(self, 'bake_drift', text='Drift', icon='FORCE_VORTEX')
        col = box.column(align=True)
        col.active = self.bake_drift
        col.prop(self, 'drift_strength')
        col.prop(self, 'drift_max_angle')
        col.prop(self, 'drift_onset')

        box = layout.box()
        box.prop(self, 'bake_steering', text='Steering', icon='DRIVER_ROTATIONAL_DIFFERENCE')
        col = box.column(align=True)
        col.active = self.bake_steering
        col.prop(self, 'rotation_factor')
        col.prop(self, 'steering_smoothing')

        box = layout.box()
        box.prop(self, 'bake_wheels', text='Wheels', icon='MESH_CYLINDER')
        col = box.column(align=True)
        col.active = self.bake_wheels
        col.prop(self, 'drive')
        col.prop(self, 'wheelspin')
        col.prop(self, 'brake_lock')
        col.prop(self, 'drift_spin')

        box = layout.box()
        box.prop(self, 'bake_suspension', text='Suspension', icon='MOD_PHYSICS')
        col = box.column(align=True)
        col.active = self.bake_suspension
        col.prop(self, 'suspension_frequency')
        col.prop(self, 'suspension_damping')
        col.prop(self, 'pitch_per_g')
        col.prop(self, 'roll_per_g')

        layout.prop(self, 'keyframe_tolerance')

    def execute(self, context):
        if self.frame_end <= self.frame_start:
            self.report({'ERROR'}, 'The end frame must come after the start frame')
            return {'CANCELLED'}
        if self._only_armature_object_moves(context):
            return {'CANCELLED'}
        if self.surface != 'CUSTOM':
            apply_surface_preset(self, context)
        self._ensure_action(context)
        rig = context.object
        car_rig.ensure_physics_channels(rig)

        scene = context.scene
        frame_current = scene.frame_current
        frames = list(range(self.frame_start, self.frame_end + 1))
        dt = 1.0 / frames_per_second(scene)
        notes = []
        try:
            if self.bake_drift:
                notes.append('drift up to %.0f°' % math.degrees(self._bake_drift(context, frames, dt)))
            if self.bake_steering:
                self._bake_steering(context)
            if self.bake_wheels:
                rig['wheels_on_y_axis'] = False
                self._bake_wheels_rotation(context, slip=physics.WheelSlip(
                    grip=self.grip, drive=self.drive, wheelspin=self.wheelspin, brake_lock=self.brake_lock,
                    drift_spin=self.drift_spin))
            if self.bake_suspension:
                pitch, roll = self._bake_suspension(context, frames, dt)
                notes.append('pitch up to %.1f°, roll up to %.1f°' % (math.degrees(pitch), math.degrees(roll)))
        finally:
            scene.frame_set(frame_current)

        surface = dict((item[0], item[1]) for item in SURFACE_ITEMS)[self.surface]
        self.report({'INFO'}, 'Baked car physics on %s%s' % (surface, (': ' + ', '.join(notes)) if notes else ''))
        return {'FINISHED'}

    def _smoothing(self, dt):
        """Frames on each side to average accelerations over, about a tenth of a second."""
        return max(1, int(round(.08 / dt)))

    @cursor('WAIT')
    def _bake_drift(self, context, frames, dt):
        """Keys the Drift control from how much each corner asks of the tyres. Returns the largest angle."""
        rig = context.object
        drift = rig.pose.bones.get('Drift')
        if drift is None or 'Root' not in rig.pose.bones:
            return 0.0
        if drift.rotation_mode in ('QUATERNION', 'AXIS_ANGLE'):
            drift.rotation_mode = 'ZYX'
        clear_baked_drift(rig, frames[0], frames[-1], keyframe_type=None)

        unit = context.scene.unit_settings.scale_length or 1.0
        samples = sample_bones(context, rig, ['Root'], frames)['Root']
        positions = [head * unit for _, head, _ in samples]
        radius = self._smoothing(dt)
        velocities = smoothed_vector_derivative(positions, dt, radius)
        accelerations = smoothed_vector_derivative(velocities, dt, radius)

        lateral, forward_speed = [], []
        for (matrix, _, _), velocity, acceleration in zip(samples, velocities, accelerations):
            # The car faces -Y: its heading is the Root bone's -Y axis, flattened onto the ground.
            heading = -matrix.col[1].xyz
            heading.z = 0
            if heading.length < 1e-6:
                lateral.append(0.0)
                forward_speed.append(0.0)
                continue
            heading.normalize()
            right = heading.cross(WORLD_UP)
            lateral.append(acceleration.dot(right))
            forward_speed.append(velocity.dot(heading))

        angles = physics.drift_angles(lateral, forward_speed, dt, self.grip, self.drift_strength,
                                      self.drift_max_angle, self.drift_onset)

        # Drift turns about its own Z axis, which points down in Rigacar's rigs: flip to the world's sense.
        drift_z = rig.matrix_world.to_3x3() @ rig.data.bones['Drift'].matrix_local.to_3x3().col[2]
        sign = 1.0 if drift_z.dot(WORLD_UP) >= 0 else -1.0
        keys = physics.reduce_keys([(f, sign * a) for f, a in zip(frames, angles)],
                                   math.radians(.05) * max(self.keyframe_tolerance, 1e-4) / .01)
        fcurve = ensure_fcurve(rig, DRIFT_DATA_PATH, DRIFT_INDEX, 'Drift')
        write_keyframes(fcurve, keys)
        return max((abs(a) for a in angles), default=0.0)

    @cursor('WAIT')
    def _bake_suspension(self, context, frames, dt):
        """Keys Body.pitch/roll/heave and Wheel.bump.* from a spring-damper body on the surface.

        Returns the largest pitch and roll it baked.
        """
        rig = context.object
        scene = context.scene
        unit = scene.unit_settings.scale_length or 1.0
        to_local = 1.0 / metres_per_unit(rig, scene)
        pose_bones = rig.pose.bones
        if 'MCH-Axis' not in pose_bones:
            return 0.0, 0.0

        suffixes = [s for s in car_rig.wheel_suffixes(rig) if 'GroundSensor.%s' % s in pose_bones]
        sensor_names = ['GroundSensor.%s' % s for s in suffixes]
        samples = sample_bones(context, rig, ['MCH-Axis'] + sensor_names, frames)

        # The kinematic chassis the rig already moves the body with.
        positions, forwards, rights = [], [], []
        kin_heave, kin_pitch, kin_roll = [], [], []
        for matrix, head, tail in samples['MCH-Axis']:
            # MCH-Axis runs from the front suspension to the back one.
            forward = (head - tail).normalized()
            up = matrix.col[2].xyz.normalized()
            right = forward.cross(up).normalized()
            centre = (head + tail) / 2 * unit
            positions.append(centre)
            forwards.append(forward)
            rights.append(right)
            kin_heave.append(centre.z)
            kin_pitch.append(math.asin(physics.clamp(forward.z, -1, 1)))
            kin_roll.append(math.asin(physics.clamp(-right.z, -1, 1)))

        radius = self._smoothing(dt)
        accelerations = smoothed_vector_derivative(smoothed_vector_derivative(positions, dt, radius), dt, radius)
        g = physics.GRAVITY
        pitch_target = [physics.soft_limit(self.pitch_per_g * a.dot(f) / g, 2 * self.pitch_per_g)
                        for a, f in zip(accelerations, forwards)]
        # Cornering pushes the body out of the turn: towards its right in a left turn.
        roll_target = [physics.soft_limit(-self.roll_per_g * a.dot(r) / g, 2 * self.roll_per_g)
                       for a, r in zip(accelerations, rights)]

        # Rough ground lifts each wheel; soft ground lets it sink. Both are fixed in the world.
        roughness, bump_size, sink = self.roughness * unit, self.bump_size * unit, self.sink * unit
        bumps = {}
        for suffix, sensor_name in zip(suffixes, sensor_names):
            bumps[suffix] = [physics.road_height(head.x * unit, head.y * unit, roughness, bump_size, self.seed) - sink
                             for _, head, _ in samples[sensor_name]]

        def group_mean(predicate, i):
            return mean(bumps[s][i] for s in suffixes if predicate(s))

        wheelbase, track = self._wheelbase_and_track(rig, suffixes, unit)
        heave_base, pitch_base, roll_base = [], [], []
        for i in range(len(frames)):
            front = group_mean(lambda s: s.startswith('Ft'), i)
            rear = group_mean(lambda s: s.startswith('Bk'), i)
            left = group_mean(lambda s: '.L' in s, i)
            right = group_mean(lambda s: '.R' in s, i)
            heave_base.append(kin_heave[i] + group_mean(lambda s: True, i))
            pitch_base.append(kin_pitch[i] + math.atan2(front - rear, wheelbase))
            roll_base.append(kin_roll[i] + math.atan2(left - right, track))

        frequency, damping = self.suspension_frequency, self.suspension_damping
        heave = physics.simulate_spring(heave_base, [0.0] * len(frames), dt, frequency, damping)
        pitch = physics.simulate_spring(pitch_base, pitch_target, dt, frequency, damping)
        roll = physics.simulate_spring(roll_base, roll_target, dt, frequency, damping)

        angle_tolerance = 1e-4 * max(self.keyframe_tolerance, 1e-4) / .01
        distance_tolerance = 2e-4 * to_local * max(self.keyframe_tolerance, 1e-4) / .01
        channels = (
            ('Body.pitch', [p - k for p, k in zip(pitch, kin_pitch)], angle_tolerance),
            ('Body.roll', [r - k for r, k in zip(roll, kin_roll)], angle_tolerance),
            ('Body.heave', [(h - k) * to_local for h, k in zip(heave, kin_heave)], distance_tolerance),
        )
        channels += tuple(('Wheel.bump.%s' % s, [b * to_local for b in bumps[s]], distance_tolerance)
                          for s in suffixes)
        for property_name, values, tolerance in channels:
            clear_property_animation(context, property_name)
            if any(abs(v) > 1e-9 for v in values):
                fcurve = create_property_animation(context, property_name, group_name='Physics')
                write_keyframes(fcurve, physics.reduce_keys(list(zip(frames, values)), tolerance))

        return max(abs(v) for v in channels[0][1]), max(abs(v) for v in channels[1][1])

    @staticmethod
    def _wheelbase_and_track(rig, suffixes, unit):
        """Distance between the axles and between the sides, in metres, from the rest pose."""
        world = rig.matrix_world
        heads = {s: world @ rig.data.bones['GroundSensor.%s' % s].head_local * unit for s in suffixes}

        def centre(predicate):
            points = [p for s, p in heads.items() if predicate(s)]
            return sum(points, mathutils.Vector()) / len(points) if points else None

        front, rear = centre(lambda s: s.startswith('Ft')), centre(lambda s: s.startswith('Bk'))
        left, right = centre(lambda s: '.L' in s), centre(lambda s: '.R' in s)
        wheelbase = (front - rear).length if front is not None and rear is not None else 0.0
        track = (left - right).length if left is not None and right is not None else 0.0
        return max(wheelbase, .1), max(track, .1)


class OBJECT_OT_carRigSetGround(bpy.types.Operator):
    bl_idname = 'object.car_rig_set_ground'
    bl_label = 'Set ground'
    bl_description = 'Makes every ground sensor follow one ground object, so the wheels ride over bumps and slopes'
    bl_options = {'REGISTER', 'UNDO'}

    ground: bpy.props.StringProperty(name='Ground',
                                     description='Mesh the car drives on. Leave empty to drive on the flat floor')
    method: bpy.props.EnumProperty(
        name='Method', default='PROJECT',
        items=[('PROJECT', 'Raycast', 'Casts each sensor straight down onto the ground, or up when it sits '
                                      'below it: best for bumps, ramps and hills'),
               ('NEAREST_SURFACE', 'Nearest Surface', "Snaps each sensor to the closest point of the ground, "
                                                      "as Rigacar always did")],
        description='How sensors find the ground')
    travel: bpy.props.FloatProperty(name='Suspension Travel', min=0, max=10, default=.2, subtype='DISTANCE',
                                    description='How far each wheel may move up or down from its axle to follow '
                                                'bumps. The axles themselves follow the larger slopes')

    @classmethod
    def poll(cls, context):
        return is_generated_car_rig(context)

    def invoke(self, context, event):
        rig = context.object
        candidates = [o for o in context.selected_objects if o is not rig and o.type == 'MESH']
        current = None
        for sensor in car_rig.enumerate_ground_sensors(rig.pose.bones):
            cns = sensor.constraints.get('Ground projection')
            if cns is not None and cns.target is not None:
                current = cns
                break
        if candidates:
            self.ground = candidates[0].name
        elif current is not None:
            self.ground = current.target.name
            self.method = current.shrinkwrap_type if current.shrinkwrap_type == 'PROJECT' else 'NEAREST_SURFACE'
        for sensor in car_rig.enumerate_ground_sensors(rig.pose.bones):
            limit = sensor.constraints.get('Ground projection limitation')
            if limit is not None:
                self.travel = max(limit.max_z, -limit.min_z)
                break
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop_search(self, 'ground', context.scene, 'objects', icon='MESH_DATA')
        layout.prop(self, 'method')
        layout.prop(self, 'travel')

    def execute(self, context):
        rig = context.object
        ground = None
        if self.ground:
            ground = context.scene.objects.get(self.ground)
            if ground is None or ground.type != 'MESH':
                self.report({'ERROR'}, 'The ground must be a mesh object of this scene')
                return {'CANCELLED'}
            if ground is rig or ground.parent is rig:
                self.report({'ERROR'}, 'The car cannot drive on itself')
                return {'CANCELLED'}

        count = 0
        for sensor in car_rig.enumerate_ground_sensors(rig.pose.bones):
            cns = sensor.constraints.get('Ground projection')
            if cns is not None:
                cns.target = ground
                cns.shrinkwrap_type = self.method
                cns.project_axis_space = 'LOCAL'
                cns.project_axis = 'NEG_Z'
                cns.use_project_opposite = True
                cns.cull_face = 'OFF'
                cns.project_limit = 0
                count += 1
            limit = sensor.constraints.get('Ground projection limitation')
            if limit is not None:
                limit.min_z = -self.travel
                limit.max_z = self.travel
        if ground is None:
            self.report({'INFO'}, 'Ground cleared from %d sensors: the car drives on the flat floor' % count)
        else:
            self.report({'INFO'}, '%d sensors follow %s' % (count, ground.name))
        return {'FINISHED'}


class OBJECT_OT_carRigFollowPath(bpy.types.Operator):
    bl_idname = 'object.car_rig_follow_path'
    bl_label = 'Follow path'
    bl_description = ('Drives the car along a curve: puts a Follow Path constraint on the Root bone and keys its '
                      'progress, with the car sitting exactly on the curve')
    bl_options = {'REGISTER', 'UNDO'}

    curve: bpy.props.StringProperty(name='Path', description='Curve the car drives along')
    frame_start: bpy.props.IntProperty(name='Start Frame', min=0, default=1)
    frame_end: bpy.props.IntProperty(name='End Frame', min=1, default=120)
    easing: bpy.props.EnumProperty(name='Speed', default='EASE',
                                   items=[('EASE', 'Ease In and Out', 'Pull away and stop smoothly'),
                                          ('LINEAR', 'Constant', 'Same speed from start to end')],
                                   description='How the speed changes along the path')
    reverse: bpy.props.BoolProperty(name='Reverse', default=False,
                                    description='Drive from the end of the curve to its start')

    @classmethod
    def poll(cls, context):
        return is_generated_car_rig(context) and context.object.mode in ('OBJECT', 'POSE')

    def invoke(self, context, event):
        rig = context.object
        curves = [o for o in context.selected_objects if o is not rig and o.type == 'CURVE']
        if curves:
            self.curve = curves[0].name
        self.frame_start, self.frame_end = context.scene.frame_start, context.scene.frame_end
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop_search(self, 'curve', context.scene, 'objects', icon='CURVE_DATA')
        col = layout.column(align=True)
        col.prop(self, 'frame_start')
        col.prop(self, 'frame_end')
        layout.prop(self, 'easing')
        layout.prop(self, 'reverse')

    def execute(self, context):
        rig = context.object
        curve = context.scene.objects.get(self.curve)
        if curve is None or curve.type != 'CURVE':
            self.report({'ERROR'}, 'Choose a curve object for the path')
            return {'CANCELLED'}
        if self.frame_end <= self.frame_start:
            self.report({'ERROR'}, 'The end frame must come after the start frame')
            return {'CANCELLED'}

        root = rig.pose.bones['Root']
        for cns in [c for c in root.constraints if c.type == 'FOLLOW_PATH']:
            root.constraints.remove(cns)
        # Follow Path adds the curve to wherever the bone already is, armature object included: with any offset
        # the car would run a parallel curve and crab through bends. So the car starts from the world origin.
        root.matrix_basis.identity()
        scale = rig.matrix_world.to_scale()
        rig.matrix_world = mathutils.Matrix.Diagonal((scale.x, scale.y, scale.z, 1.0))

        # Follow Path needs the curve's path animation, off for curves made by scripts or converted from meshes.
        curve.data.use_path = True
        cns = root.constraints.new('FOLLOW_PATH')
        cns.name = 'Follow Path'
        cns.target = curve
        cns.use_curve_follow = True
        cns.use_fixed_location = True
        cns.forward_axis = 'TRACK_NEGATIVE_Y'
        cns.up_axis = 'UP_Z'

        if rig.animation_data is None:
            rig.animation_data_create()
        if rig.animation_data.action is None:
            rig.animation_data.action = bpy.data.actions.new('%sAction' % rig.name)
        data_path = cns.path_from_id('offset_factor')
        start, end = (1.0, 0.0) if self.reverse else (0.0, 1.0)
        for frame, factor in ((self.frame_start, start), (self.frame_end, end)):
            cns.offset_factor = factor
            rig.keyframe_insert(data_path, frame=frame, group='Root')
        fcurve = ensure_fcurve(rig, data_path, 0, 'Root')
        for point in fcurve.keyframe_points:
            point.interpolation = 'BEZIER' if self.easing == 'EASE' else 'LINEAR'
            point.handle_left_type = point.handle_right_type = 'AUTO_CLAMPED'
        fcurve.update()

        curve_scale = curve.matrix_world.to_scale()
        if max(abs(abs(s) - 1.0) for s in curve_scale) > 1e-3:
            self.report({'WARNING'}, '%s is scaled, which distorts the car in bends: apply its scale '
                                     '(Object > Apply > Scale)' % curve.name)
        else:
            self.report({'INFO'}, '%s follows %s from frame %d to %d' % (rig.name, curve.name, self.frame_start,
                                                                         self.frame_end))
        return {'FINISHED'}


classes = (
    ANIM_OT_carPhysicsBake,
    OBJECT_OT_carRigSetGround,
    OBJECT_OT_carRigFollowPath,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
