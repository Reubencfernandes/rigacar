"""Renders the images and the animation of the documentation, in docs/images, from a live Blender.

Every picture comes from the add-on itself: the scenes are rigged, driven and baked with Rigacar's operators.
Run from the repository root; it needs a window, and event simulation lets it click through the interface:
    blender --factory-startup --enable-event-simulate --window-geometry 0 0 1600 1000 --python docs/capture.py
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

import bpy
import gpu
from mathutils import Matrix, Vector

DOCS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(DOCS)
IMAGES = os.path.join(DOCS, 'images')
sys.path.insert(0, os.path.join(REPO, 'tests'))
import e2e  # noqa: E402

# Kenney's sedan is toy-sized: scaled up it is 4.5 m long, like a real saloon, so the physics reads true.
CAR_SCALE = 1.75
FPS = 24
ONLY = {name.strip() for name in os.environ.get('RIGACAR_CAPTURE', '').split(',') if name.strip()}

SURFACES = (
    ('ASPHALT', 'ASPHALT', (.32, .33, .35, 1)),
    ('GRAVEL', 'GRAVEL', (.62, .56, .46, 1)),
    ('MUD', 'MUD', (.36, .25, .16, 1)),
    ('ICE', 'ICE', (.72, .86, .95, 1)),
)
GHOST = (.86, .18, .16, 1)


def log(message):
    print('[capture] %s' % message, flush=True)


def main_view():
    window = bpy.context.window_manager.windows[0]
    area = max((a for a in window.screen.areas if a.type == 'VIEW_3D'), key=lambda a: a.width * a.height)
    region = next(r for r in area.regions if r.type == 'WINDOW')
    return window, area, region


def in_view():
    window, area, region = main_view()
    return bpy.context.temp_override(window=window, area=area, region=region)


def link(obj):
    bpy.context.scene.collection.objects.link(obj)
    return obj


def look_at(obj, eye, target):
    obj.location = eye
    obj.rotation_euler = (Vector(target) - Vector(eye)).to_track_quat('-Z', 'Y').to_euler()


def new_camera(eye, target, lens=40, ortho_scale=None):
    data = bpy.data.cameras.new('DocCamera')
    data.lens = lens
    data.clip_end = 500
    if ortho_scale:
        data.type = 'ORTHO'
        data.ortho_scale = ortho_scale
    camera = link(bpy.data.objects.new('DocCamera', data))
    look_at(camera, eye, target)
    bpy.context.scene.camera = camera
    return camera


def setup_view(color_type='TEXTURE', overlays=True, floor=False):
    _, area, _ = main_view()
    space = area.spaces.active
    shading = space.shading
    shading.type = 'SOLID'
    shading.light = 'STUDIO'
    shading.color_type = color_type
    shading.show_shadows = True
    shading.shadow_intensity = .35
    shading.show_cavity = True
    shading.cavity_type = 'BOTH'
    shading.use_world_space_lighting = False
    # Solid mode casts its shadows from this light: high overhead, they stay under the car.
    display = bpy.context.scene.display
    display.light_direction = Vector((-.25, .3, .92)).normalized()
    display.shadow_shift = .1
    shading.background_type = 'VIEWPORT'
    shading.background_color = (.93, .94, .95)
    overlay = space.overlay
    overlay.show_overlays = overlays
    overlay.show_floor = floor
    overlay.show_axis_x = overlay.show_axis_y = False
    overlay.show_relationship_lines = False
    overlay.show_extras = False
    overlay.show_cursor = False
    overlay.show_text = False
    overlay.show_stats = False
    space.show_gizmo = False
    space.show_region_ui = False
    space.show_region_toolbar = False
    space.show_region_header = True


def capture(path, camera, width, height):
    """Draws the viewport through a camera, off screen, at any size: what Blender shows, without its interface."""
    window, area, region = main_view()
    space = area.spaces.active
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    view_matrix = camera.matrix_world.inverted()
    projection = camera.calc_matrix_camera(depsgraph, x=width, y=height)
    offscreen = gpu.types.GPUOffScreen(width, height)
    with bpy.context.temp_override(window=window, area=area, region=region):
        offscreen.draw_view3d(scene, bpy.context.view_layer, space, region, view_matrix, projection,
                              do_color_management=True, draw_background=True)
        with offscreen.bind():
            pixels = gpu.state.active_framebuffer_get().read_color(0, 0, width, height, 4, 0, 'FLOAT')
    offscreen.free()
    pixels.dimensions = width * height * 4
    image = bpy.data.images.new('capture', width, height, alpha=False)
    image.pixels.foreach_set(pixels)
    image.filepath_raw = path
    image.file_format = 'PNG'
    image.save()
    bpy.data.images.remove(image)
    if os.path.dirname(path) == IMAGES:
        log('saved docs/images/%s' % os.path.basename(path))


def material(name, colour, roughness=.8):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = colour
    mat.roughness = roughness
    return mat


def import_car(offset=(0, 0, 0)):
    """Kenney's sedan, renamed for Rigacar, scaled to a real car and moved by offset."""
    body, wheels = e2e.import_sedan()
    transform = Matrix.Translation(offset) @ Matrix.Scale(CAR_SCALE, 4)
    for obj in (body, *wheels):
        obj.matrix_world = transform @ obj.matrix_world
    bpy.context.view_layer.update()
    return body, wheels


def rig_car(body, wheels):
    e2e.select_only([body, *wheels], body)
    with in_view():
        bpy.ops.object.armature_car_deformation_rig('INVOKE_DEFAULT')
        rig = bpy.context.object
        bpy.ops.pose.car_animation_rig_generate()
        bpy.ops.object.mode_set(mode='OBJECT')
    return rig


def car_parts(body, wheels):
    return [body, *body.children, *wheels]


def add_bend_path(name, origin=(0, 0, 0), lead=12.0, radius=10.0, exit=12.0, bevel=.06):
    """A curve straight ahead (the car faces -Y), through a 90° left-hander, then straight out along +X.

    Four control points carry the handles of a true quarter circle. Automatic handles through many points
    wobble in curvature between them, and the car's heading and steering follow every wobble.
    Returns the curve object and its length.
    """
    ox, oy, oz = origin
    handle = radius * 4 / 3 * math.tan(math.pi / 8)
    ahead, along = Vector((0, -1, 0)), Vector((1, 0, 0))
    start = Vector((ox, oy, oz))
    arc_in = start + ahead * lead
    arc_out = arc_in + ahead * radius + along * radius
    end = arc_out + along * exit
    controls = (
        (start, start - ahead * lead / 3, start + ahead * lead / 3),
        (arc_in, arc_in - ahead * lead / 3, arc_in + ahead * handle),
        (arc_out, arc_out - along * handle, arc_out + along * exit / 3),
        (end, end - along * exit / 3, end + along * exit / 3),
    )
    data = bpy.data.curves.new(name, 'CURVE')
    data.dimensions = '3D'
    data.resolution_u = 64
    data.bevel_depth = bevel
    spline = data.splines.new('BEZIER')
    spline.bezier_points.add(len(controls) - 1)
    for point, (co, left, right) in zip(spline.bezier_points, controls):
        point.handle_left_type = point.handle_right_type = 'FREE'
        point.co, point.handle_left, point.handle_right = co, left, right
    obj = link(bpy.data.objects.new(name, data))
    obj.color = (1, 1, 1, 1)
    return obj, lead + math.pi / 2 * radius + exit


def follow_and_bake(rig, path, frames, surface, easing='LINEAR', **bake_options):
    bpy.context.view_layer.objects.active = rig
    with in_view():
        bpy.ops.object.car_rig_follow_path('EXEC_DEFAULT', curve=path.name, frame_start=1, frame_end=frames,
                                           easing=easing)
        bpy.ops.anim.car_physics_bake('EXEC_DEFAULT', frame_start=1, frame_end=frames, surface=surface,
                                      **bake_options)


def ghost(parts, frame, colour, prefix):
    """Frozen copies of the car's meshes as they are at a frame, for a trail of the motion."""
    scene = bpy.context.scene
    scene.frame_set(frame)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    copies = []
    for obj in parts:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(evaluated)
        copy = link(bpy.data.objects.new('%s.%s.%03d' % (prefix, obj.name, frame), mesh))
        copy.matrix_world = evaluated.matrix_world.copy()
        copy.color = colour
        copies.append(copy)
    return copies


def remove_car(rig, parts):
    for obj in [rig, *parts]:
        bpy.data.objects.remove(obj)
    for block in (bpy.data.meshes, bpy.data.armatures, bpy.data.actions):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


def ground(name, size, location, colour, subdivisions=0, displace=0.0, noise_size=4.0):
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=max(subdivisions, 1), y_subdivisions=max(subdivisions, 1),
                                    size=size, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.color = colour
    obj.data.materials.append(material(name, colour))
    if displace:
        texture = bpy.data.textures.new(name, 'CLOUDS')
        texture.noise_scale = noise_size
        texture.noise_depth = 1
        modifier = obj.modifiers.new('Bumps', 'DISPLACE')
        modifier.texture = texture
        modifier.strength = displace
        modifier.mid_level = .5
        obj.data.polygons.foreach_set('use_smooth', [True] * len(obj.data.polygons))
    return obj


def label(text, location, size=2.2, colour=(.1, .1, .12, 1)):
    data = bpy.data.curves.new(text, 'FONT')
    data.body = text
    data.size = size
    data.align_x = 'CENTER'
    obj = link(bpy.data.objects.new('Label.' + text, data))
    obj.location = location
    obj.color = colour
    return obj


# ---------------------------------------------------------------------------------------------------------------
# Scenes

def scene_rig_controls():
    """The rigged sedan at rest, its controls on show, front wheels steered."""
    e2e.reset_scene()
    body, wheels = import_car()
    rig = rig_car(body, wheels)
    floor = ground('Floor', 400, (0, 0, 0), (.8, .8, .82, 1))
    floor.select_set(False)
    rig.show_in_front = True
    bpy.context.view_layer.objects.active = rig
    with in_view():
        bpy.ops.object.mode_set(mode='POSE')
        bpy.ops.pose.select_all(action='DESELECT')
    rig.pose.bones['Steering'].location.x = .35
    setup_view('TEXTURE')
    camera = new_camera((7.5, -7.2, 4.2), (0, -.2, .55), lens=42)
    yield .5
    capture(os.path.join(IMAGES, 'rig-controls.png'), camera, 1600, 900)
    with in_view():
        bpy.ops.object.mode_set(mode='OBJECT')


def scene_drift_surfaces():
    """The same bend at the same speed on four surfaces, seen from above, as a trail of the car."""
    e2e.reset_scene()
    spacing = 30.0
    frames = 0
    for index, (surface, title, colour) in enumerate(SURFACES):
        origin = Vector(((index % 2) * spacing, -(index // 2) * spacing, 0))
        ground('Ground.' + surface, spacing - 1, origin + Vector((11, -13, -.01)), colour).select_set(False)
        path, length = add_bend_path('Path.' + surface, origin)
        # 8.5 m/s through a 10 m bend asks for 0.74 g of grip: asphalt holds it, looser ground lets go.
        frames = int(round(length / 8.5 * FPS))
        body, wheels = import_car(origin)
        rig = rig_car(body, wheels)
        follow_and_bake(rig, path, frames, surface)
        parts = car_parts(body, wheels)
        for frame in range(12, frames - 6, 11):
            ghost(parts, frame, GHOST, surface)
        remove_car(rig, parts)
        label(title, origin + Vector((16, -3.5, .05)), colour=(1, 1, 1, 1) if surface in ('MUD', 'ASPHALT') else (.1, .1, .12, 1))
        log('baked %s over %d frames' % (surface, frames))
        yield .1
    setup_view('OBJECT', overlays=False)
    camera = new_camera((26, -28, 80), (26, -28, 0), ortho_scale=61)
    yield .3
    capture(os.path.join(IMAGES, 'drift-surfaces.png'), camera, 1400, 1400)


def build_mud_bend():
    """A muddy, bumpy bend driven with physics: the scene of the animation and of the interface."""
    e2e.reset_scene()
    terrain = ground('Terrain', 120, (12, -16, 0), (.38, .27, .18, 1), subdivisions=300, displace=.45,
                     noise_size=3.5)
    terrain.select_set(False)
    path, length = add_bend_path('Path', (0, 4, 0), lead=16, radius=11, exit=14, bevel=0)
    path.hide_set(True)
    frames = int(round(length / 9.0 * FPS))
    bpy.context.scene.frame_start, bpy.context.scene.frame_end = 1, frames
    body, wheels = import_car()
    rig = rig_car(body, wheels)
    bpy.context.view_layer.objects.active = rig
    with in_view():
        bpy.ops.object.car_rig_set_ground('EXEC_DEFAULT', ground=terrain.name, travel=.35)
    follow_and_bake(rig, path, frames, 'MUD', easing='EASE')
    return rig, body, wheels, terrain, frames


def scene_mud_animation(rig, body, frames):
    """An animated GIF of the car sliding through the muddy bend, from a camera that turns with it."""
    rig.show_in_front = False
    rig.hide_set(True)
    setup_view('TEXTURE', overlays=False)
    scene = bpy.context.scene
    eye = Vector((11, -12, 11))
    camera = new_camera(eye, body.matrix_world.translation, lens=30)
    keep = os.environ.get('RIGACAR_FRAMES')
    folder = keep or tempfile.mkdtemp(prefix='rigacar-gif-')
    os.makedirs(folder, exist_ok=True)
    for frame in range(1, frames + 1):
        scene.frame_set(frame)
        look_at(camera, eye, body.matrix_world.translation + Vector((0, 0, .4)))
        bpy.context.view_layer.update()
        capture(os.path.join(folder, 'frame_%04d.png' % frame), camera, 800, 450)
        if frame % 20 == 0:
            yield .01
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        make_gif(ffmpeg, folder, os.path.join(IMAGES, 'mud-drift.gif'))
    else:
        log('ffmpeg not found: skipped the GIF')
    if not keep:
        shutil.rmtree(folder, ignore_errors=True)
    rig.hide_set(False)


def make_gif(ffmpeg, folder, path, fps=12, width=640):
    graph = ('fps=%d,scale=%d:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];'
             '[b][p]paletteuse=dither=none:diff_mode=rectangle' % (fps, width))
    subprocess.run([ffmpeg, '-y', '-loglevel', 'error', '-framerate', str(FPS), '-i',
                    os.path.join(folder, 'frame_%04d.png'), '-vf', graph, '-loop', '0', path], check=True)
    log('saved docs/images/%s (%.1f MB)' % (os.path.basename(path), os.path.getsize(path) / 1e6))


def sculpted_ground(name, size, colour, features, subdivisions=160, noise=.04):
    """A grid lifted by smooth mounds, (x, y, height, radius) each, over a little noise."""
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=subdivisions, y_subdivisions=subdivisions, size=size)
    obj = bpy.context.object
    obj.name = name
    obj.color = colour
    obj.data.materials.append(material(name, colour))
    physics = sys.modules['rigacar'].physics
    for vertex in obj.data.vertices:
        x, y = vertex.co.x, vertex.co.y
        height = physics.road_height(x, y, noise, 1.2, 3)
        for fx, fy, fh, fr in features:
            height += fh * math.exp(-((x - fx) ** 2 + (y - fy) ** 2) / (fr * fr))
        vertex.co.z = height
    obj.data.polygons.foreach_set('use_smooth', [True] * len(obj.data.polygons))
    obj.data.update()
    obj.select_set(False)
    return obj


def scene_terrain():
    """The car parked over a mound and a dip: each wheel at its own height, the body tilted between them."""
    e2e.reset_scene()
    body, wheels = import_car()
    # The scaled sedan's wheels touch the ground at x = +/-0.53, y = +/-1.16: a mound under the right front wheel,
    # a dip under the right rear one, seen from the right side.
    terrain = sculpted_ground('Terrain', 40, (.66, .58, .46, 1),
                              features=((-.53, -1.16, .34, .7), (-.53, 1.16, -.24, .75)))
    rig = rig_car(body, wheels)
    bpy.context.view_layer.objects.active = rig
    with in_view():
        bpy.ops.object.car_rig_set_ground('EXEC_DEFAULT', ground=terrain.name, travel=.4)
    rig.show_in_front = True
    with in_view():
        bpy.ops.object.mode_set(mode='POSE')
        bpy.ops.pose.select_all(action='DESELECT')
    setup_view('TEXTURE')
    camera = new_camera((-9.5, -1.6, 1.0), (0, -.15, .75), lens=42)
    yield .4
    capture(os.path.join(IMAGES, 'terrain.png'), camera, 1600, 900)
    with in_view():
        bpy.ops.object.mode_set(mode='OBJECT')


def crop(path, left, right, top, bottom):
    """Keeps a rectangle of a saved image; its edges are in pixels from the left and from the top."""
    import numpy
    image = bpy.data.images.load(path)
    width, height = image.size
    left, right = max(0, left), min(width, right)
    top, bottom = max(0, top), min(height, bottom)
    pixels = numpy.empty(width * height * 4, dtype=numpy.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(height, width, 4)[height - bottom:height - top, left:right]
    cropped = bpy.data.images.new('crop', right - left, bottom - top, alpha=False)
    cropped.pixels.foreach_set(numpy.ascontiguousarray(pixels).ravel())
    cropped.filepath_raw = path
    cropped.file_format = 'PNG'
    cropped.save()
    bpy.data.images.remove(image)
    bpy.data.images.remove(cropped)


def click(window, x, y):
    window.event_simulate('MOUSEMOVE', 'NOTHING', x=x, y=y)
    window.event_simulate('LEFTMOUSE', 'PRESS', x=x, y=y)
    window.event_simulate('LEFTMOUSE', 'RELEASE', x=x, y=y)


def open_sidebar_tab(window, sidebar, category):
    """Clicks down the sidebar's tabs until the wanted one is active: Blender has no call to switch tabs."""
    x = sidebar.x + sidebar.width - 10
    for y in range(sidebar.y + sidebar.height - 12, sidebar.y + 20, -14):
        if sidebar.active_panel_category == category:
            break
        click(window, x, y)
        yield .15
    log('sidebar tab: %s' % sidebar.active_panel_category)


def frame_car(space, region, body, offset_pixels, distance=10.0):
    """Looks at the car from three quarters above, with the car offset_pixels to the right of the centre."""
    view = space.region_3d
    view.view_perspective = 'PERSP'
    view.view_rotation = (Matrix.Rotation(math.radians(-38), 4, 'Z') @
                          Matrix.Rotation(math.radians(63), 4, 'X')).to_quaternion()
    view.view_distance = distance
    # The viewport's lens is 50 mm on a 72 mm sensor.
    metres_per_pixel = distance * 72 / 50 / region.width
    right = view.view_rotation @ Vector((1, 0, 0))
    view.view_location = body.matrix_world.translation - right * (offset_pixels * metres_per_pixel)


def scene_interface(rig, body, frames):
    """The Rigacar tab of the sidebar, and the physics bake dialog."""
    window, area, region = main_view()
    space = area.spaces.active
    scene = bpy.context.scene
    scene.frame_set(int(frames * .6))
    rig.show_in_front = True
    setup_view('TEXTURE')
    space.show_region_header = True
    space.show_region_ui = True
    bpy.context.view_layer.objects.active = rig
    with in_view():
        bpy.ops.object.mode_set(mode='POSE')
        bpy.ops.pose.select_all(action='DESELECT')
    frame_car(space, region, body, 120)
    yield .5
    sidebar = next(r for r in area.regions if r.type == 'UI')
    yield from open_sidebar_tab(window, sidebar, 'Rigacar')
    # Park the mouse over the viewport, so no button is highlighted.
    window.event_simulate('MOUSEMOVE', 'NOTHING', x=region.x + 80, y=region.y + 80)
    yield .6
    path = os.path.join(IMAGES, 'ui-sidebar.png')
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.screen.screenshot_area(filepath=path)
    crop(path, area.width - 880, area.width, 0, 620)
    log('saved docs/images/ui-sidebar.png')

    # The bake dialog opens under the mouse: beside the car, clear of the sidebar.
    frame_car(space, region, body, -340, distance=12.5)
    mouse_x, mouse_y = region.x + int(region.width * .52), region.y + int(region.height * .6)
    window.event_simulate('MOUSEMOVE', 'NOTHING', x=mouse_x, y=mouse_y)
    yield .4
    with in_view():
        bpy.ops.anim.car_physics_bake('INVOKE_DEFAULT', surface='MUD')
    yield 1.0
    path = os.path.join(IMAGES, 'ui-physics-bake.png')
    with bpy.context.temp_override(window=window):
        bpy.ops.screen.screenshot(filepath=path)
    crop(path, area.x, area.x + area.width, 0, window.height)
    log('saved docs/images/ui-physics-bake.png')
    window.event_simulate('ESC', 'PRESS', x=mouse_x, y=mouse_y)
    yield .5
    with in_view():
        bpy.ops.object.mode_set(mode='OBJECT')


def scene_curves():
    """Launch, corner and brake on flat, loose ground, read in the Graph Editor: pitch, roll and drift."""
    e2e.reset_scene()
    window, area, region = main_view()
    scene = bpy.context.scene
    frames = 200
    scene.frame_start, scene.frame_end = 1, frames
    path, _ = add_bend_path('Path', lead=15, radius=25, exit=15, bevel=0)
    body, wheels = import_car()
    rig = rig_car(body, wheels)
    follow_and_bake(rig, path, frames, 'CUSTOM', easing='EASE', grip=.5, roughness=0, sink=0)
    scene.frame_set(1)

    bake_operators = sys.modules['rigacar'].bake_operators
    animation_data = rig.animation_data
    slot = getattr(animation_data, 'action_slot', None)
    fcurves = bake_operators.action_fcurves(animation_data.action, slot)
    colours = {'["Body.pitch"]': (1.0, .55, .1), '["Body.roll"]': (.25, .6, 1.0),
               'pose.bones["Drift"].rotation_euler': (.35, .9, .35)}
    for fcurve in fcurves:
        fcurve.hide = fcurve.data_path not in colours
        fcurve.select = False
        if not fcurve.hide:
            fcurve.color_mode = 'CUSTOM'
            fcurve.color = colours[fcurve.data_path]
        if fcurve.group is not None:
            fcurve.group.show_expanded = fcurve.group.show_expanded_graph = not fcurve.hide
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    area.type = 'GRAPH_EDITOR'
    yield .3
    space = area.spaces.active
    space.use_normalization = True
    space.show_region_ui = False
    # Bone channels such as the Drift control's only show for selected bones otherwise.
    space.dopesheet.show_only_selected = False
    graph_region = next(r for r in area.regions if r.type == 'WINDOW')
    channels = next((r for r in area.regions if r.type == 'CHANNELS'), None)
    if channels is not None:
        for _ in range(3):
            with bpy.context.temp_override(window=window, area=area, region=channels):
                bpy.ops.anim.channels_expand(all=True)
    with bpy.context.temp_override(window=window, area=area, region=graph_region):
        bpy.ops.graph.view_all()
    window.event_simulate('MOUSEMOVE', 'NOTHING', x=graph_region.x + 40, y=graph_region.y + 40)
    yield .6
    path = os.path.join(IMAGES, 'physics-curves.png')
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.screen.screenshot_area(filepath=path)
    log('saved docs/images/physics-curves.png')
    area.type = 'VIEW_3D'
    yield .3


def steps():
    os.makedirs(IMAGES, exist_ok=True)
    e2e.import_addon()
    bpy.context.preferences.view.show_splash = False

    def wanted(name):
        return not ONLY or name in ONLY

    if wanted('controls'):
        yield from scene_rig_controls()
    if wanted('terrain'):
        yield from scene_terrain()
    if wanted('drift'):
        yield from scene_drift_surfaces()
    if wanted('curves'):
        yield from scene_curves()
    if wanted('interface') or wanted('gif'):
        rig, body, wheels, terrain, frames = build_mud_bend()
        yield .2
        if wanted('interface'):
            yield from scene_interface(rig, body, frames)
        if wanted('gif'):
            yield from scene_mud_animation(rig, body, frames)


def run():
    generator = steps()

    def tick():
        try:
            return max(.01, next(generator))
        except StopIteration:
            log('done')
        except Exception:
            traceback.print_exc()
            log('failed')
        sys.stdout.flush()
        os._exit(0)

    bpy.app.timers.register(tick, first_interval=.5)


if bpy.app.background:
    print('[capture] needs a window: run without --background', flush=True)
    os._exit(2)
bpy.context.preferences.view.show_splash = False
run()
