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

# Read by Blender when installed as a legacy add-on; blender_manifest.toml replaces it for extensions.
bl_info = {
    "name": "Rigacar (Generates Car Rig)",
    "author": "David Gayerie, Webber Takken, Reuben Fernandes",
    "version": (9, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Add > Armature",
    "description": "Adds a deformation rig for vehicles, generates the animation rig and bakes wheels, steering, "
                   "drift and suspension physics.",
    "doc_url": "https://github.com/Reubencfernandes/rigacar#readme",
    "tracker_url": "https://github.com/Reubencfernandes/rigacar/issues",
    "category": "Rigging"
}

if "bpy" in locals():
    import importlib

    for module_name in ("physics", "bake_operators", "car_rig", "widgets", "physics_operators"):
        if module_name in locals():
            importlib.reload(locals()[module_name])
else:
    import bpy
    from . import physics
    from . import bake_operators
    from . import car_rig
    from . import physics_operators

# Kept here for scripts that imported it from the package before it moved to car_rig.
enumerate_ground_sensors = car_rig.enumerate_ground_sensors


class RIGACAR_PT_mixin:
    # Panels must not override __init__: Blender 4.4 and later construct them with arguments.

    @classmethod
    def is_car_rig(cls, context):
        obj = getattr(context, 'object', None)
        return obj is not None and obj.type == 'ARMATURE' and obj.data is not None and 'Car Rig' in obj.data

    @classmethod
    def is_car_rig_generated(cls, context):
        return cls.is_car_rig(context) and bool(context.object.data['Car Rig'])

    def prepare_layout(self):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        return self.layout

    def display_generate_section(self, context):
        self.layout.operator(car_rig.POSE_OT_carAnimationRigGenerate.bl_idname, text='Generate',
                             icon='OUTLINER_OB_ARMATURE')

    def display_bake_section(self, context):
        layout = self.layout
        layout.operator(physics_operators.ANIM_OT_carPhysicsBake.bl_idname, text='Bake Car Physics', icon='PHYSICS')
        col = layout.column(align=True)
        col.operator(bake_operators.ANIM_OT_carSteeringBake.bl_idname, text='Bake Steering Only')
        col.operator(bake_operators.ANIM_OT_carWheelsRotationBake.bl_idname, text='Bake Wheels Only')
        layout.operator(bake_operators.ANIM_OT_carClearSteeringWheelsRotation.bl_idname, icon='TRASH')

    def display_rig_props_section(self, context):
        self.layout.operator(physics_operators.OBJECT_OT_carRigFollowPath.bl_idname, text='Follow Path',
                             icon='CURVE_PATH')
        layout = self.prepare_layout().column()
        layout.prop(context.object, '["wheels_on_y_axis"]', text="Wheels on Y axis")
        layout.prop(context.object, '["suspension_factor"]', text="Pitch factor")
        layout.prop(context.object, '["suspension_rolling_factor"]', text="Roll factor")

    def display_physics_section(self, context):
        layout = self.prepare_layout()
        obj = context.object
        layout.operator(physics_operators.OBJECT_OT_carRigSetGround.bl_idname, text='Set Ground', icon='MESH_GRID')
        if all(name in obj.keys() for name in ('Body.pitch', 'Body.roll', 'Body.heave')):
            col = layout.column(align=True)
            col.prop(obj, '["Body.pitch"]', text='Body Pitch')
            col.prop(obj, '["Body.roll"]', text='Roll')
            col.prop(obj, '["Body.heave"]', text='Heave')
        else:
            layout.label(text='Physics channels appear on the first physics bake', icon='INFO')

    def display_ground_sensors_section(self, context):
        layout = self.prepare_layout()
        for ground_sensor in car_rig.enumerate_ground_sensors(context.object.pose.bones):
            ground_projection_constraint = ground_sensor.constraints.get('Ground projection')
            layout.label(text=ground_sensor.name, icon='BONE_DATA')
            if ground_projection_constraint is not None:
                layout.prop(ground_projection_constraint, 'target', text='Ground')
                if ground_projection_constraint.target is not None:
                    layout.prop(ground_projection_constraint, 'shrinkwrap_type')
                    if ground_projection_constraint.shrinkwrap_type == 'PROJECT':
                        layout.prop(ground_projection_constraint, 'project_limit')
                    layout.prop(ground_projection_constraint, 'influence')
            ground_projection_limit_constraint = ground_sensor.constraints.get('Ground projection limitation')
            if ground_projection_limit_constraint is not None:
                layout.prop(ground_projection_limit_constraint, 'min_z', text='Min local Z')
                layout.prop(ground_projection_limit_constraint, 'max_z', text='Max local Z')
            layout.separator()


class RIGACAR_PT_rigProperties(bpy.types.Panel, RIGACAR_PT_mixin):
    bl_label = "Rigacar"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "data"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return RIGACAR_PT_mixin.is_car_rig(context)

    def draw(self, context):
        if RIGACAR_PT_mixin.is_car_rig_generated(context):
            self.display_rig_props_section(context)
            self.layout.separator()
            self.display_bake_section(context)
        else:
            self.display_generate_section(context)


class RIGACAR_PT_physicsProperties(bpy.types.Panel, RIGACAR_PT_mixin):
    bl_label = "Physics"
    bl_parent_id = "RIGACAR_PT_rigProperties"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "data"

    @classmethod
    def poll(cls, context):
        return RIGACAR_PT_mixin.is_car_rig_generated(context)

    def draw(self, context):
        self.display_physics_section(context)


class RIGACAR_PT_groundSensorsProperties(bpy.types.Panel, RIGACAR_PT_mixin):
    bl_label = "Ground Sensors"
    bl_parent_id = "RIGACAR_PT_rigProperties"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "data"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return RIGACAR_PT_mixin.is_car_rig_generated(context)

    def draw(self, context):
        self.display_ground_sensors_section(context)


class RIGACAR_PT_animationRigView(bpy.types.Panel, RIGACAR_PT_mixin):
    bl_category = "Rigacar"
    bl_label = "Animation Rig"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    @classmethod
    def poll(cls, context):
        return RIGACAR_PT_mixin.is_car_rig(context)

    def draw(self, context):
        if RIGACAR_PT_mixin.is_car_rig_generated(context):
            self.display_rig_props_section(context)
        else:
            self.display_generate_section(context)


class RIGACAR_PT_wheelsAnimationView(bpy.types.Panel, RIGACAR_PT_mixin):
    bl_category = "Rigacar"
    bl_label = "Bake Animation"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    @classmethod
    def poll(cls, context):
        return RIGACAR_PT_mixin.is_car_rig_generated(context)

    def draw(self, context):
        self.display_bake_section(context)


class RIGACAR_PT_physicsView(bpy.types.Panel, RIGACAR_PT_mixin):
    bl_category = "Rigacar"
    bl_label = "Physics"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    @classmethod
    def poll(cls, context):
        return RIGACAR_PT_mixin.is_car_rig_generated(context)

    def draw(self, context):
        self.display_physics_section(context)


class RIGACAR_PT_groundSensorsView(bpy.types.Panel, RIGACAR_PT_mixin):
    bl_category = "Rigacar"
    bl_label = "Ground Sensors"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return RIGACAR_PT_mixin.is_car_rig_generated(context)

    def draw(self, context):
        self.display_ground_sensors_section(context)


def menu_entries(menu, context):
    menu.layout.operator(car_rig.OBJECT_OT_armatureCarDeformationRig.bl_idname, text="Car (deformation rig)",
                         icon='AUTO')


classes = (
    RIGACAR_PT_rigProperties,
    RIGACAR_PT_physicsProperties,
    RIGACAR_PT_groundSensorsProperties,
    RIGACAR_PT_animationRigView,
    RIGACAR_PT_wheelsAnimationView,
    RIGACAR_PT_physicsView,
    RIGACAR_PT_groundSensorsView,
)


def register():
    bpy.types.VIEW3D_MT_armature_add.append(menu_entries)
    for c in classes:
        bpy.utils.register_class(c)
    car_rig.register()
    bake_operators.register()
    physics_operators.register()


def unregister():
    physics_operators.unregister()
    bake_operators.unregister()
    car_rig.unregister()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    bpy.types.VIEW3D_MT_armature_add.remove(menu_entries)


if __name__ == "__main__":
    register()
