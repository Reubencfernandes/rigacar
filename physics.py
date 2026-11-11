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

"""Vehicle dynamics behind Rigacar's physics bake.

Plain Python with no dependency on Blender, so the models can be tested anywhere.
Every quantity is in SI units: metres, seconds, radians.

The models are deliberately simple and art-directable rather than a full tyre
simulation. Rigacar animates a car along a path chosen by the animator; physics
adds the secondary motion a real car would show along that path:

* the body is a sprung mass that lags, overshoots and settles (spring-damper),
* it squats under acceleration, dives under braking and rolls in corners,
* the rear slides out when cornering asks for more grip than the surface has,
* driven wheels spin on loose ground and lock when braking on slippery ground,
* rough ground shakes the wheels and, filtered by the springs, the body.
"""

import math

GRAVITY = 9.81

# id: (label, grip, roughness (m), bump size (m), sink (m), description)
SURFACES = {
    'ASPHALT': ('Asphalt', 1.0, 0.0, 2.0, 0.0, 'Dry tarmac: full grip, smooth'),
    'WET': ('Wet Asphalt', 0.7, 0.0, 2.0, 0.0, 'Wet tarmac: less grip, smooth'),
    'GRAVEL': ('Gravel', 0.6, 0.012, 0.6, 0.01, 'Loose gravel: fine vibration, slides easily'),
    'DIRT': ('Dirt Track', 0.55, 0.025, 1.2, 0.015, 'Dirt road: bumps and potholes, loose grip'),
    'MUD': ('Mud', 0.35, 0.03, 1.5, 0.06, 'Mud: little grip, ruts, the tyres sink in'),
    'SAND': ('Sand', 0.45, 0.02, 2.5, 0.05, 'Sand: soft dunes, the tyres sink in'),
    'SNOW': ('Snow', 0.25, 0.01, 1.0, 0.04, 'Packed snow: slippery, the tyres sink a little'),
    'ICE': ('Ice', 0.1, 0.0, 2.0, 0.0, 'Ice: almost no grip, smooth'),
}

DRIVES = ('FWD', 'RWD', 'AWD')


def clamp(value, low, high):
    return max(low, min(high, value))


def soft_limit(value, limit):
    """Smoothly saturates value to +/-limit, linear around zero."""
    if limit <= 0:
        return 0.0
    return limit * math.tanh(value / limit)


def smooth(values, radius):
    """Centred moving average; the window shrinks at both ends so the series keeps its length."""
    if radius <= 0 or len(values) < 3:
        return list(values)
    count = len(values)
    result = []
    for i in range(count):
        low, high = max(0, i - radius), min(count, i + radius + 1)
        result.append(sum(values[low:high]) / (high - low))
    return result


def derivative(values, dt):
    """Central differences, one-sided at both ends."""
    count = len(values)
    if count < 2:
        return [0.0] * count
    result = [(values[1] - values[0]) / dt]
    for i in range(1, count - 1):
        result.append((values[i + 1] - values[i - 1]) / (2 * dt))
    result.append((values[-1] - values[-2]) / dt)
    return result


def vector_derivative(points, dt):
    columns = list(zip(*points)) if points else ((), (), ())
    derived = [derivative(list(column), dt) for column in columns]
    return [tuple(v) for v in zip(*derived)]


def simulate_spring(base, target, dt, frequency, damping, substeps=8):
    """Sprung mass following a moving base: x'' = w^2 (base + target - x) + 2 z w (base' - x').

    base is the road (or kinematic chassis) input, target a static offset such as
    weight transfer. Starts at rest in equilibrium. Returns x for every frame.
    """
    count = len(base)
    if count == 0:
        return []
    omega = 2 * math.pi * max(frequency, 1e-3)
    zeta = max(damping, 0.0)
    base_speed = derivative(base, dt)
    x = base[0] + target[0]
    v = base_speed[0]
    result = [x]
    h = dt / substeps
    for i in range(1, count):
        for step in range(1, substeps + 1):
            t = step / substeps
            b = base[i - 1] + (base[i] - base[i - 1]) * t
            g = target[i - 1] + (target[i] - target[i - 1]) * t
            bv = (base[i] - base[i - 1]) / dt
            acceleration = omega * omega * (b + g - x) + 2 * zeta * omega * (bv - v)
            v += acceleration * h
            x += v * h
        result.append(x)
    return result


def _hash(ix, iy, seed):
    h = (ix * 374761393 + iy * 668265263 + (seed + 1) * 1442695041) & 0xffffffff
    h = ((h ^ (h >> 13)) * 1274126177) & 0xffffffff
    h ^= h >> 16
    return (h & 0xffff) / 32767.5 - 1.0


def value_noise(x, y, seed=0):
    """Smooth 2D value noise in [-1, 1]."""
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    sx, sy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    a, b = _hash(ix, iy, seed), _hash(ix + 1, iy, seed)
    c, d = _hash(ix, iy + 1, seed), _hash(ix + 1, iy + 1, seed)
    top = a + (b - a) * sx
    bottom = c + (d - c) * sx
    return top + (bottom - top) * sy


def road_height(x, y, roughness, bump_size, seed=0):
    """Height of rough ground at a world position, in metres.

    The ground is fixed in the world, so a rear wheel meets the bump a front wheel
    met a moment earlier, and a car standing still does not shake.
    """
    if roughness <= 0:
        return 0.0
    size = max(bump_size, 1e-3)
    u, v = x / size, y / size
    noise = .7 * value_noise(u, v, seed) + .3 * value_noise(u * 2.7 + 17.1, v * 2.7 - 9.3, seed + 1)
    return roughness * noise


def drift_angles(lateral_acceleration, forward_speed, dt, grip, strength=1.0, max_angle=math.radians(40),
                 onset=.8, frequency=1.0, damping=.7):
    """Drift (yaw of the body away from its path, counter-clockwise seen from above) for every frame.

    Tyres hold a lateral acceleration up to grip * g. Past onset * grip * g the rear
    starts to slide outward, more so the further the corner asks beyond grip. A
    spring-damper eases the car into the slide and lets it swing back out of it.
    lateral_acceleration is measured along the car's right: negative in a left turn.
    """
    limit = max(grip, 1e-3) * GRAVITY
    threshold = limit * onset
    targets = []
    for acceleration, speed in zip(lateral_acceleration, forward_speed):
        excess = abs(acceleration) - threshold
        # No slide when parking or reversing; full slide from a brisk 3 m/s on.
        speed_factor = clamp((speed - 1.0) / 2.0, 0.0, 1.0)
        if excess <= 0 or speed_factor <= 0:
            targets.append(0.0)
            continue
        angle = max_angle * math.tanh(strength * excess / limit) * speed_factor
        # The rear swings out of the turn: a left turn (acceleration to the left) yaws the body further left.
        targets.append(-math.copysign(angle, acceleration))
    zeros = [0.0] * len(targets)
    return simulate_spring(zeros, targets, dt, frequency, damping)


class WheelSlip(object):
    """How a wheel's spin departs from pure rolling on a given surface."""

    # Seconds of acceleration turned into wheelspin speed on fully loose ground.
    SPIN_TIME = .6
    # Time constant of the tyre's slip, smoothing flicker from noisy keyframes.
    RESPONSE = .12

    def __init__(self, grip=1.0, drive='RWD', wheelspin=1.0, brake_lock=1.0, drift_spin=1.0):
        self.grip = grip
        self.drive = drive
        self.wheelspin = wheelspin
        self.brake_lock = brake_lock
        self.drift_spin = drift_spin

    @property
    def looseness(self):
        return clamp(1.0 - self.grip, 0.0, 1.0)

    def is_driven(self, position):
        """position is 'Ft' or 'Bk'."""
        return self.drive == 'AWD' or (self.drive == 'FWD') == (position == 'Ft')

    def surface_speeds(self, ground_speeds, lateral_speeds, dt, driven):
        """Speed of the tyre's surface, in m/s, from the wheel's ground speed along its heading.

        A rolling wheel's surface moves at its ground speed. A driven wheel spins faster
        when the car accelerates on loose ground or slides sideways under power; any
        wheel turns slower, down to locking, when braking on slippery ground.
        """
        accelerations = smooth(derivative(ground_speeds, dt), 2)
        limit = max(self.grip, 1e-3) * GRAVITY
        alpha = 1.0 - math.exp(-dt / self.RESPONSE)
        slip = 0.0
        lock = 0.0
        result = []
        for speed, lateral, acceleration in zip(ground_speeds, lateral_speeds, accelerations):
            if abs(speed) > .05:
                direction = math.copysign(1.0, speed)
            elif abs(acceleration) > 1e-6:
                direction = math.copysign(1.0, acceleration)
            else:
                direction = 1.0
            forward_acceleration = acceleration * direction
            target_slip = 0.0
            target_lock = 0.0
            if driven and forward_acceleration > 0:
                target_slip += self.wheelspin * self.looseness * forward_acceleration * self.SPIN_TIME
            if driven and abs(speed) > .5:
                target_slip += self.drift_spin * abs(lateral)
            if forward_acceleration < 0:
                target_lock = clamp(self.brake_lock * self.looseness * -forward_acceleration / limit, 0.0, 1.0)
            slip += (target_slip - slip) * alpha
            lock += (target_lock - lock) * alpha
            result.append(direction * (abs(speed) * (1.0 - lock) + slip))
        return result


def reduce_keys(points, tolerance):
    """Ramer-Douglas-Peucker on (frame, value) points: drops points linear interpolation recovers."""
    if len(points) <= 2 or tolerance <= 0:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        (x0, y0), (x1, y1) = points[first], points[last]
        worst, worst_index = -1.0, None
        for i in range(first + 1, last):
            x, y = points[i]
            t = (x - x0) / (x1 - x0) if x1 != x0 else 0.0
            error = abs(y - (y0 + (y1 - y0) * t))
            if error > worst:
                worst, worst_index = error, i
        if worst_index is not None and worst > tolerance:
            keep[worst_index] = True
            stack.append((first, worst_index))
            stack.append((worst_index, last))
    return [p for p, k in zip(points, keep) if k]
