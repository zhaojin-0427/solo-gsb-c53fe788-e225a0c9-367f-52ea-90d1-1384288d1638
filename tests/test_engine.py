import unittest
from fractions import Fraction

from app.engine import (
    EngineError,
    Item,
    evaluate,
    referrer_chain,
    resolve,
    round_half_up,
)


def abs_item(id, fixture="A", channel=1, target=100, duration=1000, ms=0):
    return Item(id, fixture, channel, target, duration, "abs", start_ms=ms)


def ref_item(id, ref_id, edge="start", offset=0, fixture="A", channel=1,
             target=100, duration=1000):
    return Item(id, fixture, channel, target, duration, "ref",
                ref_id=ref_id, ref_edge=edge, ref_offset=offset)


class TestRounding(unittest.TestCase):
    def test_half_up(self):
        self.assertEqual(round_half_up(Fraction(0)), 0)
        self.assertEqual(round_half_up(Fraction(1, 3)), 0)
        self.assertEqual(round_half_up(Fraction(1, 2)), 1)
        self.assertEqual(round_half_up(Fraction(2, 3)), 1)
        self.assertEqual(round_half_up(Fraction(3, 2)), 2)
        self.assertEqual(round_half_up(Fraction(2545, 10)), 255)


class TestValidation(unittest.TestCase):
    def test_missing_reference(self):
        with self.assertRaisesRegex(EngineError, "引用缺失"):
            resolve([abs_item(1), ref_item(2, 99)])

    def test_self_cycle(self):
        with self.assertRaisesRegex(EngineError, "环"):
            resolve([ref_item(1, 1)])

    def test_two_node_cycle(self):
        with self.assertRaisesRegex(EngineError, "环"):
            resolve([ref_item(1, 2), ref_item(2, 1)])

    def test_three_node_cycle(self):
        # 1 -> 2(end) -> 3(end) -> 1(start): all on distinct channels so
        # the cycle is the only violation
        with self.assertRaisesRegex(EngineError, "环"):
            resolve([ref_item(1, 2, channel=1),
                     ref_item(2, 3, channel=2),
                     ref_item(3, 1, channel=3)])

    def test_same_channel_same_instant_abs(self):
        with self.assertRaisesRegex(EngineError, "同时起步"):
            resolve([abs_item(1, ms=10), abs_item(2, ms=10)])

    def test_same_channel_same_instant_via_reference(self):
        # item 1 ends at 100; item 2 starts at 100 absolute; item 3
        # references item 1's end, also at 100 -> collision
        with self.assertRaisesRegex(EngineError, "同时起步"):
            resolve([abs_item(1, ms=0, duration=100),
                     abs_item(2, ms=100, target=10),
                     ref_item(3, 1, "end", target=20)])

    def test_different_channel_same_instant_ok(self):
        resolve([abs_item(1, channel=1, ms=10), abs_item(2, channel=2, ms=10)])

    def test_non_adjacent_same_instant_rejected(self):
        # starts 0, 500, 0 on the same channel: a "last value" check would
        # miss the collision between items 1 and 3
        with self.assertRaisesRegex(EngineError, "同时起步"):
            resolve([abs_item(1, ms=0), abs_item(2, ms=500),
                     abs_item(3, ms=0, target=20)])


class TestReferences(unittest.TestCase):
    def test_start_and_end_offsets(self):
        view = resolve([
            abs_item(1, duration=400, ms=100),       # 100-500
            ref_item(2, 1, "start", 50),             # 150-1150
            ref_item(3, 1, "end", 25, channel=2),    # 525-1525
        ])
        r = {ri.item.id: ri for ri in view.items}
        self.assertEqual((r[1].start, r[1].end), (100, 500))
        self.assertEqual((r[2].start, r[2].end), (150, 1150))
        self.assertEqual((r[3].start, r[3].end), (525, 1525))

    def test_chain_order(self):
        view = resolve([
            ref_item(3, 2, "end", 10, duration=100),
            ref_item(2, 1, "end", 10, duration=100),
            abs_item(1, ms=0, duration=100),
        ])
        r = {ri.item.id: ri for ri in view.items}
        self.assertEqual(r[1].start, 0)
        self.assertEqual(r[2].start, 110)
        self.assertEqual(r[3].start, 220)


class TestFades(unittest.TestCase):
    def test_linear_basic_and_initial_zero(self):
        view = resolve([abs_item(1, target=100, duration=1000, ms=0)])
        vals = {t: evaluate(view, t)["A|1"]["value"]
                for t in (0, 250, 500, 750, 1000, 2000)}
        self.assertEqual(vals, {0: 0, 250: 25, 500: 50, 750: 75,
                                1000: 100, 2000: 100})

    def test_zero_duration_instant(self):
        view = resolve([abs_item(1, target=200, duration=0, ms=50)])
        self.assertEqual(evaluate(view, 49)["A|1"]["value"], 0)
        self.assertEqual(evaluate(view, 50)["A|1"]["value"], 200)
        self.assertEqual(evaluate(view, 1000)["A|1"]["value"], 200)

    def test_continue_from_unrounded_value(self):
        # 0..3 over 10 ms -> 0.3/ms; interrupted at 5ms at value 1.5
        view = resolve([
            abs_item(1, target=3, duration=10, ms=0),
            abs_item(2, target=103, duration=100, ms=5),
        ])
        at5 = evaluate(view, 5)["A|1"]
        self.assertEqual(at5["raw"], "3/2")
        self.assertEqual(at5["value"], 2)  # 1.5 rounds up
        # new fade starts from 1.5: at 55ms (halfway to 103) -> 52.25
        at55 = evaluate(view, 55)["A|1"]
        self.assertEqual(at55["raw"], "209/4")
        self.assertEqual(at55["value"], 52)
        # at 105ms the fade finishes exactly at 103
        at105 = evaluate(view, 105)["A|1"]
        self.assertEqual(at105["raw"], "103")
        self.assertEqual(at105["value"], 103)

    def test_interrupted_fade_marked(self):
        view = resolve([
            abs_item(1, target=100, duration=1000, ms=0),
            abs_item(2, target=0, duration=100, ms=500),
        ])
        r = {ri.item.id: ri for ri in view.items}
        self.assertEqual(r[1].interrupted_by, 2)
        self.assertIsNone(r[2].interrupted_by)
        # at 600ms the second fade (500..600, 50->0) is ending at 0
        self.assertEqual(evaluate(view, 600)["A|1"]["value"], 0)

    def test_back_to_back_not_interrupted(self):
        view = resolve([
            abs_item(1, target=100, duration=1000, ms=0),
            abs_item(2, target=200, duration=1000, ms=1000),
        ])
        r = {ri.item.id: ri for ri in view.items}
        self.assertIsNone(r[1].interrupted_by)
        self.assertEqual(evaluate(view, 1000)["A|1"]["value"], 100)

    def test_multiple_interruptions(self):
        view = resolve([
            abs_item(1, target=100, duration=1000, ms=0),
            abs_item(2, target=200, duration=1000, ms=200),
            abs_item(3, target=0, duration=1000, ms=400),
        ])
        # value at 200 = 20, then fade to 200; at 400 -> 20 + 180*0.2 = 56
        self.assertEqual(evaluate(view, 400)["A|1"]["raw"], "56")
        # fade to 0: at 1400 -> 0
        self.assertEqual(evaluate(view, 1400)["A|1"]["value"], 0)
        r = {ri.item.id: ri for ri in view.items}
        self.assertEqual(r[1].interrupted_by, 2)
        self.assertEqual(r[2].interrupted_by, 3)

    def test_instant_then_fade(self):
        view = resolve([
            abs_item(1, target=80, duration=0, ms=100),
            abs_item(2, target=180, duration=100, ms=200),
        ])
        self.assertEqual(evaluate(view, 250)["A|1"]["raw"], "130")

    def test_channels_independent(self):
        view = resolve([
            abs_item(1, fixture="L1", channel=1, target=100, duration=100, ms=0),
            abs_item(2, fixture="L1", channel=2, target=200, duration=100, ms=50),
        ])
        self.assertEqual(evaluate(view, 50)["L1|1"]["value"], 50)
        self.assertEqual(evaluate(view, 50)["L1|2"]["value"], 0)


class TestFractionalTimes(unittest.TestCase):
    def test_decimal_time_linear(self):
        # 0 -> 3 over 10 ms, evaluated at a fractional millisecond
        view = resolve([abs_item(1, target=3, duration=10, ms=0)])
        at = evaluate(view, Fraction(3, 2))["A|1"]  # t = 1.5
        self.assertEqual(at["raw"], "9/20")          # 3 * 1.5/10
        self.assertEqual(at["value"], 0)
        at_frac = evaluate(view, Fraction(5, 3))["A|1"]
        self.assertEqual(at_frac["raw"], "1/2")
        self.assertEqual(at_frac["value"], 1)          # 0.5 rounds up
        # flooring the same instant would wrongly yield 3/10
        floored = evaluate(view, 1)["A|1"]
        self.assertEqual(floored["raw"], "3/10")
        self.assertNotEqual(floored["raw"], at["raw"])

    def test_half_up_at_fractional_instant(self):
        # the half-up boundary at the *value* level must be reachable
        view = resolve([abs_item(1, target=100, duration=1000, ms=0)])
        at = evaluate(view, Fraction(5))["A|1"]           # value 0.5
        self.assertEqual(at["raw"], "1/2")
        self.assertEqual(at["value"], 1)                     # exact .5 -> up
        before = evaluate(view, Fraction(4495, 100))["A|1"]  # 44.95ms -> 4.495
        self.assertEqual(before["raw"], "899/200")
        self.assertEqual(before["value"], 4)
        after = evaluate(view, Fraction(4505, 100))["A|1"]   # 45.05ms -> 4.505
        self.assertEqual(after["value"], 5)

    def test_integer_times_still_exact(self):
        view = resolve([abs_item(1, target=100, duration=1000, ms=0)])
        for t in (0, 250, 500, 1000, 2000):
            ch = evaluate(view, t)["A|1"]
            expected = Fraction(100 * min(t, 1000), 1000)
            self.assertEqual(Fraction(ch["raw"]), expected)

    def test_interrupt_continues_at_fractional_boundary(self):
        # 0..3 over 10 ms, cut at t=5 (value exactly 3/2); the next fade
        # 3/2 -> 103 over 100 ms, probed at 5.5 ms
        view = resolve([
            abs_item(1, target=3, duration=10, ms=0),
            abs_item(2, target=103, duration=100, ms=5),
        ])
        at = evaluate(view, Fraction(11, 2))["A|1"]      # t = 5.5
        # 3/2 + (103 - 3/2) * 0.5/100 = 3/2 + 203/400 = 803/400
        self.assertEqual(at["raw"], "803/400")
        self.assertEqual(at["active_item_id"], 2)

    def test_instant_snap_at_fractional_time_below(self):
        view = resolve([abs_item(1, target=200, duration=0, ms=5)])
        self.assertEqual(evaluate(view, Fraction(99, 20))["A|1"]["value"], 0)  # 4.95
        self.assertEqual(evaluate(view, Fraction(5))["A|1"]["value"], 200)       # 5
        self.assertEqual(evaluate(view, Fraction(501, 100))["A|1"]["value"], 200)

    def test_negative_fraction_rejected(self):
        view = resolve([abs_item(1)])
        with self.assertRaisesRegex(EngineError, "时间不能为负"):
            evaluate(view, Fraction(-1, 2))


class TestReferrerChain(unittest.TestCase):
    def test_closure(self):
        items = [
            abs_item(1),
            ref_item(2, 1),
            ref_item(3, 2),
            ref_item(4, 1),
            abs_item(5),
        ]
        self.assertEqual(sorted(referrer_chain(items, 1)), [2, 3, 4])
        self.assertEqual(referrer_chain(items, 5), [])


if __name__ == "__main__":
    unittest.main()
