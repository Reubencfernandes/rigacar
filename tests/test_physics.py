"""Unit tests of the vehicle dynamics in physics.py. Plain Python, no Blender needed:
    python -m unittest discover -s tests -p "test_*.py"
"""
import importlib.util
import math
import os
import unittest

_spec = importlib.util.spec_from_file_location(
    'rigacar_physics', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'physics.py'))
physics = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(physics)

DT = 1 / 24


class SpringTest(unittest.TestCase):

    def test_rests_in_equilibrium(self):
        x = physics.simulate_spring([1.0] * 48, [.5] * 48, DT, 1.5, .5)
        self.assertTrue(all(abs(v - 1.5) < 1e-9 for v in x))

    def test_step_overshoots_then_settles(self):
        base = [0.0] * 12 + [1.0] * 108
        x = physics.simulate_spring(base, [0.0] * len(base), DT, 1.5, .3)
        self.assertGreater(max(x), 1.05, 'an underdamped body overshoots a step')
        self.assertLess(abs(x[-1] - 1.0), .01, 'and settles on it')

    def test_critical_damping_does_not_overshoot_a_load(self):
        # Weight transfer moves the target, not the road: a critically damped body eases onto it.
        target = [0.0] * 12 + [1.0] * 108
        x = physics.simulate_spring([0.0] * len(target), target, DT, 1.5, 1.0)
        self.assertLess(max(x), 1.0 + 1e-3)
        self.assertGreater(x[-1], .99)

    def test_kerb_jolts_through_the_damper(self):
        # The damper passes the road's sudden rise on: even at critical damping the body overshoots a kerb.
        base = [0.0] * 12 + [1.0] * 108
        x = physics.simulate_spring(base, [0.0] * len(base), DT, 1.5, 1.0)
        self.assertGreater(max(x), 1.0)
        self.assertLess(abs(x[-1] - 1.0), .01)

    def test_holds_a_steady_target(self):
        x = physics.simulate_spring([0.0] * 200, [0.0] * 10 + [.1] * 190, DT, 2, .7)
        self.assertAlmostEqual(x[-1], .1, places=3)


class DriftTest(unittest.TestCase):

    def corner(self, lateral, speed, grip):
        frames = 96
        return physics.drift_angles([lateral] * frames, [speed] * frames, DT, grip)

    def test_no_drift_within_grip(self):
        self.assertEqual(max(map(abs, self.corner(-4.9, 6, 1.0))), 0)

    def test_drifts_on_mud(self):
        angles = self.corner(-4.9, 6, .35)
        self.assertGreater(angles[-1], math.radians(10), 'a left turn on mud yaws the body further left')

    def test_ice_drifts_more_than_mud(self):
        self.assertGreater(self.corner(-4.9, 6, .1)[-1], self.corner(-4.9, 6, .35)[-1])

    def test_right_turn_drifts_the_other_way(self):
        self.assertLess(self.corner(4.9, 6, .35)[-1], 0)

    def test_no_drift_when_parking(self):
        self.assertEqual(max(map(abs, self.corner(-8, .5, .1))), 0)

    def test_angle_is_bounded(self):
        angles = physics.drift_angles([-50] * 200, [20] * 200, DT, .1, max_angle=math.radians(40))
        self.assertLess(max(map(abs, angles)), math.radians(46))


class WheelSlipTest(unittest.TestCase):

    def launch(self):
        # Accelerates from standstill at 3 m/s² for two seconds.
        return [3.0 * i * DT for i in range(48)]

    def test_grip_rolls_exactly(self):
        speeds = self.launch()
        slip = physics.WheelSlip(grip=1.0)
        out = slip.surface_speeds(speeds, [0.0] * len(speeds), DT, driven=True)
        self.assertTrue(all(abs(a - b) < 1e-9 for a, b in zip(speeds, out)))

    def test_driven_wheels_spin_on_mud(self):
        speeds = self.launch()
        slip = physics.WheelSlip(grip=.35, drive='RWD')
        driven = slip.surface_speeds(speeds, [0.0] * len(speeds), DT, driven=True)
        free = slip.surface_speeds(speeds, [0.0] * len(speeds), DT, driven=False)
        self.assertGreater(sum(driven), sum(speeds) * 1.1)
        self.assertAlmostEqual(sum(free), sum(speeds), places=6)

    def test_wheels_lock_braking_on_ice(self):
        speeds = [10 - 1.0 * i * DT for i in range(96)]
        slip = physics.WheelSlip(grip=.1)
        out = slip.surface_speeds(speeds, [0.0] * len(speeds), DT, driven=False)
        self.assertLess(sum(out), sum(speeds) * .5)

    def test_drive_layouts(self):
        self.assertTrue(physics.WheelSlip(drive='RWD').is_driven('Bk'))
        self.assertFalse(physics.WheelSlip(drive='RWD').is_driven('Ft'))
        self.assertTrue(physics.WheelSlip(drive='FWD').is_driven('Ft'))
        self.assertFalse(physics.WheelSlip(drive='FWD').is_driven('Bk'))
        self.assertTrue(physics.WheelSlip(drive='AWD').is_driven('Ft'))
        self.assertTrue(physics.WheelSlip(drive='AWD').is_driven('Bk'))

    def test_reversing_spins_backwards(self):
        speeds = [-2.0] * 24
        out = physics.WheelSlip(grip=.35).surface_speeds(speeds, [0.0] * 24, DT, driven=True)
        self.assertTrue(all(v < 0 for v in out))


class RoadTest(unittest.TestCase):

    def test_smooth_road_is_flat(self):
        self.assertEqual(physics.road_height(3.2, -1.7, 0, 1), 0)

    def test_rough_road_is_bounded_and_varies(self):
        heights = [physics.road_height(x * .1, 0, .03, 1.5) for x in range(500)]
        self.assertLessEqual(max(map(abs, heights)), .03 + 1e-9)
        self.assertGreater(max(heights) - min(heights), .02)

    def test_road_is_fixed_in_the_world(self):
        self.assertEqual(physics.road_height(1.234, 5.678, .03, 1.5, 7),
                         physics.road_height(1.234, 5.678, .03, 1.5, 7))

    def test_road_is_continuous(self):
        a = physics.road_height(2.0, 3.0, .03, 1.5)
        b = physics.road_height(2.001, 3.0, .03, 1.5)
        self.assertLess(abs(a - b), 1e-3)


class ReduceKeysTest(unittest.TestCase):

    def test_line_keeps_ends(self):
        points = [(f, 2.0 * f) for f in range(100)]
        self.assertEqual(physics.reduce_keys(points, 1e-6), [points[0], points[-1]])

    def test_keeps_corners(self):
        points = [(f, abs(f - 50)) for f in range(101)]
        self.assertIn((50, 0), physics.reduce_keys(points, 1e-6))

    def test_error_within_tolerance(self):
        points = [(f, math.sin(f / 10)) for f in range(200)]
        kept = physics.reduce_keys(points, .01)
        self.assertLess(len(kept), len(points))
        for x, y in points:
            for (x0, y0), (x1, y1) in zip(kept, kept[1:]):
                if x0 <= x <= x1:
                    self.assertLess(abs(y - (y0 + (y1 - y0) * (x - x0) / (x1 - x0))), .01 + 1e-9)
                    break


if __name__ == '__main__':
    unittest.main()
