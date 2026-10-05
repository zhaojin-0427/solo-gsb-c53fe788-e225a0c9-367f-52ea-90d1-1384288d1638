import os
import tempfile
import unittest
from fractions import Fraction

os.environ["LIGHTS_DB"] = os.path.join(tempfile.mkdtemp(), "test_api.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.engine import Item, evaluate, resolve  # noqa: E402
from app.main import app  # noqa: E402


def abs_body(fixture="L1", channel=1, target=100, duration=1000, ms=0):
    return {"fixture": fixture, "channel": channel, "target": target,
            "duration": duration, "start_mode": "abs", "start_ms": ms}


def ref_body(ref_id, edge="start", offset=0, fixture="L1", channel=1,
             target=100, duration=1000):
    return {"fixture": fixture, "channel": channel, "target": target,
            "duration": duration, "start_mode": "ref",
            "ref_id": ref_id, "ref_edge": edge, "ref_offset": offset}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = TestClient(app)

    def setUp(self):
        # fresh empty project per test
        r = self.client.post("/api/projects", json={"name": "t"})
        self.pid = r.json()["id"]
        self.base = f"/api/projects/{self.pid}"

    def test_create_and_resolve(self):
        r = self.client.post(f"{self.base}/items", json=abs_body(ms=100, duration=200))
        self.assertEqual(r.status_code, 201, r.text)
        it = r.json()["items"][0]
        self.assertEqual(it["actual_start"], 100)
        self.assertEqual(it["actual_end"], 300)

    def test_missing_reference_rejected_and_unchanged(self):
        self.client.post(f"{self.base}/items", json=abs_body())
        before = self.client.get(self.base).json()
        r = self.client.post(f"{self.base}/items", json=ref_body(999))
        self.assertEqual(r.status_code, 400)
        self.assertIn("引用缺失", r.json()["detail"])
        after = self.client.get(self.base).json()
        self.assertEqual(len(after["items"]), 1)
        self.assertEqual(before, after)

    def test_cycle_rejected(self):
        a = self.client.post(f"{self.base}/items", json=abs_body(channel=1)).json()
        id_a = a["items"][0]["id"]
        b = self.client.post(f"{self.base}/items",
                             json=ref_body(id_a, channel=2)).json()
        id_b = b["items"][-1]["id"]
        r = self.client.put(f"{self.base}/items/{id_a}",
                            json=ref_body(id_b, channel=1))
        self.assertEqual(r.status_code, 400)
        self.assertIn("环", r.json()["detail"])
        # item a still absolute
        cur = self.client.get(self.base).json()["items"]
        self.assertEqual(cur[0]["start_mode"], "abs")

    def test_same_instant_rejected(self):
        self.client.post(f"{self.base}/items", json=abs_body(ms=50))
        r = self.client.post(f"{self.base}/items", json=abs_body(ms=50, target=20))
        self.assertEqual(r.status_code, 400)
        self.assertIn("同时起步", r.json()["detail"])
        self.assertEqual(len(self.client.get(self.base).json()["items"]), 1)

    def test_delete_referenced_rejected(self):
        a = self.client.post(f"{self.base}/items", json=abs_body()).json()
        id_a = a["items"][0]["id"]
        self.client.post(f"{self.base}/items", json=ref_body(id_a, channel=2))
        r = self.client.delete(f"{self.base}/items/{id_a}")
        self.assertEqual(r.status_code, 409)
        self.assertIn("引用", r.json()["detail"])
        self.assertEqual(len(self.client.get(self.base).json()["items"]), 2)

    def test_delete_then_ok(self):
        a = self.client.post(f"{self.base}/items", json=abs_body()).json()
        id_a = a["items"][0]["id"]
        r = self.client.delete(f"{self.base}/items/{id_a}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"], [])

    def test_recalculate_after_edit(self):
        a = self.client.post(f"{self.base}/items",
                             json=abs_body(ms=0, duration=100)).json()
        id_a = a["items"][0]["id"]
        b = self.client.post(f"{self.base}/items",
                             json=ref_body(id_a, "end", offset=10, channel=2)).json()
        id_b = b["items"][-1]["id"]
        self.assertEqual(b["items"][-1]["actual_start"], 110)
        r = self.client.put(f"{self.base}/items/{id_a}",
                            json=abs_body(ms=0, duration=250))
        self.assertEqual(r.status_code, 200)
        b_after = next(i for i in r.json()["items"] if i["id"] == id_b)
        self.assertEqual(b_after["actual_start"], 260)

    def test_eval_matches_engine(self):
        a = self.client.post(f"{self.base}/items",
                             json=abs_body(target=3, duration=10)).json()
        id_a = a["items"][0]["id"]
        self.client.post(f"{self.base}/items",
                         json=abs_body(target=103, duration=100, ms=5))
        r = self.client.get(f"{self.base}/eval", params={"t": 4})
        ch = r.json()["channels"][0]
        self.assertEqual(ch["raw"], "6/5")
        self.assertEqual(ch["value"], 1)
        self.assertEqual(ch["active_item_id"], id_a)
        r2 = self.client.get(f"{self.base}/eval", params={"t": 5})
        ch2 = r2.json()["channels"][0]
        self.assertEqual(ch2["raw"], "3/2")
        self.assertEqual(ch2["value"], 2)

    def test_eval_decimal_time_end_to_end(self):
        self.client.post(f"{self.base}/items",
                        json=abs_body(target=3, duration=10))
        # t=1.5: 3 * 1.5/10 = 9/20, value 0; flooring to 1 would give 3/10
        r = self.client.get(f"{self.base}/eval?t=1.5")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["time_ms"], 1.5)
        ch = body["channels"][0]
        self.assertEqual(ch["raw"], "9/20")
        self.assertEqual(ch["value"], 0)
        # t=5 reaches the half-up point: 3/2 -> displayed 2
        r5 = self.client.get(f"{self.base}/eval?t=5").json()
        self.assertEqual(r5["time_ms"], 5)  # integral echo stays a JSON int
        self.assertEqual(r5["channels"][0]["raw"], "3/2")
        self.assertEqual(r5["channels"][0]["value"], 2)

    def test_eval_fraction_literal(self):
        self.client.post(f"{self.base}/items",
                        json=abs_body(target=3, duration=10))
        r = self.client.get(f"{self.base}/eval", params={"t": "5/3"})
        self.assertEqual(r.status_code, 200, r.text)
        ch = r.json()["channels"][0]
        self.assertEqual(ch["raw"], "1/2")
        self.assertEqual(ch["value"], 1)  # 0.5 rounds half up

    def test_eval_decimal_matches_integer_regression(self):
        self.client.post(f"{self.base}/items",
                         json=abs_body(target=100, duration=1000))
        for t, expected in ((0, 0), (250, 25), (500, 50),
                              (1000, 100), (2000, 100)):
            r = self.client.get(f"{self.base}/eval", params={"t": t})
            self.assertEqual(r.json()["time_ms"], t)
            self.assertEqual(r.json()["channels"][0]["value"], expected)

    def test_eval_rejects_bad_times(self):
        for bad in ("-1.5", "abc", "", "-0.1"):
            r = self.client.get(f"{self.base}/eval", params={"t": bad})
            self.assertEqual(r.status_code, 422, bad)
        # missing parameter is also a validation error
        self.assertEqual(self.client.get(f"{self.base}/eval").status_code, 422)

    def test_field_validation(self):
        r = self.client.post(f"{self.base}/items",
                             json=abs_body(target=256))
        self.assertEqual(r.status_code, 422)
        r = self.client.post(f"{self.base}/items",
                             json=abs_body(duration=-1))
        self.assertEqual(r.status_code, 422)
        r = self.client.post(f"{self.base}/items",
                             json={**abs_body(), "start_mode": "ref",
                                   "ref_id": 1, "ref_edge": "middle"})
        self.assertEqual(r.status_code, 422)

    def test_interrupted_flag(self):
        a = self.client.post(f"{self.base}/items",
                             json=abs_body(target=100, duration=1000)).json()
        id_a = a["items"][0]["id"]
        b = self.client.post(f"{self.base}/items",
                             json=abs_body(target=0, duration=100, ms=500)).json()
        id_b = b["items"][-1]["id"]
        items = {i["id"]: i for i in b["items"]}
        self.assertEqual(items[id_a]["interrupted_by"], id_b)
        self.assertIsNone(items[id_b]["interrupted_by"])

    def test_eval_high_precision_time_verbatim(self):
        # more than 6 fractional digits must reach the engine untouched —
        # no fixed 6-decimal rounding anywhere before evaluation
        self.client.post(f"{self.base}/items",
                         json=abs_body(target=100, duration=1000))
        r = self.client.get(f"{self.base}/eval", params={"t": "1.23456789"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["time_ms"], 1.23456789)
        ch = body["channels"][0]
        # 100 * t / 1000 with t = 123456789/100000000, computed exactly
        self.assertEqual(ch["raw"], "123456789/1000000000")
        self.assertEqual(ch["value"], 0)
        # the old 6-decimal rounding of the same moment would differ
        self.assertNotEqual(ch["raw"], str(Fraction("1.234568") / 10))

    def test_eval_high_precision_matches_direct_eval(self):
        # interface result == direct engine evaluation at the same instant,
        # including a probe just past an interruption boundary
        a = self.client.post(f"{self.base}/items",
                             json=abs_body(target=3, duration=10)).json()
        id_a = a["items"][0]["id"]
        b = self.client.post(f"{self.base}/items",
                             json=abs_body(target=103, duration=100, ms=5)).json()
        id_b = b["items"][-1]["id"]
        view = resolve([
            Item(id_a, "L1", 1, 3, 10, "abs", start_ms=0),
            Item(id_b, "L1", 1, 103, 100, "abs", start_ms=5),
        ])
        for text in ("1.23456789", "5.000000001", "7.5", "105"):
            t = Fraction(text)
            direct = evaluate(view, t)["L1|1"]
            r = self.client.get(f"{self.base}/eval", params={"t": text})
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["time_ms"], float(text) if t.denominator != 1
                             else t.numerator)
            ch = body["channels"][0]
            self.assertEqual(ch["raw"], direct["raw"], text)
            self.assertEqual(ch["value"], direct["value"], text)
            self.assertEqual(ch["active_item_id"], direct["active_item_id"], text)


class TransplantApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = TestClient(app)

    def _project(self, name="t"):
        return self.client.post("/api/projects", json={"name": name}).json()["id"]

    def _add(self, pid, body):
        r = self.client.post(f"/api/projects/{pid}/items", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["items"][-1]["id"]

    def _source(self):
        """Graph with a shared ancestor and start/end offsets."""
        s = self._project("S")
        a = self._add(s, abs_body(ms=200, duration=100, fixture="F", channel=1))
        b = self._add(s, {"fixture": "F", "channel": 2, "target": 50,
                          "duration": 50, "start_mode": "ref",
                          "ref_id": a, "ref_edge": "end", "ref_offset": 10})
        d = self._add(s, {"fixture": "F", "channel": 3, "target": 50,
                          "duration": 50, "start_mode": "ref",
                          "ref_id": b, "ref_edge": "start", "ref_offset": 5})
        e = self._add(s, {"fixture": "F", "channel": 4, "target": 50,
                          "duration": 50, "start_mode": "ref",
                          "ref_id": a, "ref_edge": "start", "ref_offset": 7})
        return s, (a, b, d, e)

    def test_transplant_basic_alignment_and_id_map(self):
        s, (a, b, d, e) = self._source()
        t = self._project("T")
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [d, e], "landing_ms": 1000})
        self.assertEqual(r.status_code, 201, r.text)
        p = r.json()
        self.assertEqual(p["id"], t)
        self.assertEqual(p["landing_ms"], 1000)
        self.assertEqual(p["shift_ms"], 800)
        self.assertEqual(p["source_project_id"], s)
        self.assertEqual(len(p["id_map"]), 4)  # a pulled once, shared
        by_old = {m["source_id"]: m["new_id"] for m in p["id_map"]}
        items = {i["id"]: i for i in p["items"]}
        # earliest actual start (a) lands exactly on the landing point
        self.assertEqual(items[by_old[a]]["actual_start"], 1000)
        # relative differences preserved
        self.assertEqual(items[by_old[b]]["actual_start"], 1110)
        self.assertEqual(items[by_old[d]]["actual_start"], 1115)
        self.assertEqual(items[by_old[e]]["actual_start"], 1007)
        self.assertEqual(items[by_old[d]]["actual_end"], 1165)
        # references rewired to the new ids, start/end semantics kept
        self.assertEqual(items[by_old[b]]["ref_id"], by_old[a])
        self.assertEqual(items[by_old[b]]["ref_edge"], "end")
        self.assertEqual(items[by_old[b]]["ref_offset"], 10)
        self.assertEqual(items[by_old[d]]["ref_id"], by_old[b])
        self.assertEqual(items[by_old[e]]["ref_id"], by_old[a])

    def test_source_project_unchanged_after_success(self):
        s, (a, b, d, e) = self._source()
        t = self._project("T2")
        before = self.client.get(f"/api/projects/{s}").json()
        self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [d, e], "landing_ms": 0})
        after = self.client.get(f"/api/projects/{s}").json()
        self.assertEqual(before, after)

    def test_target_existing_items_unchanged_fields(self):
        s, (a, b, d, e) = self._source()
        t = self._project("T3")
        x = self._add(t, abs_body(ms=50, duration=77, fixture="Z", channel=9,
                                  target=123))
        before = [i for i in self.client.get(f"/api/projects/{t}").json()["items"]
                  if i["id"] == x]
        self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [e], "landing_ms": 5000})
        after = [i for i in self.client.get(f"/api/projects/{t}").json()["items"]
                 if i["id"] == x]
        self.assertEqual(before, after)

    def test_transplant_into_empty_target_landing_zero(self):
        s, (a, b, d, e) = self._source()
        t = self._project("Empty")
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [a], "landing_ms": 0})
        self.assertEqual(r.status_code, 201, r.text)
        it = r.json()["items"][0]
        self.assertEqual(it["actual_start"], 0)
        self.assertEqual(it["actual_end"], 100)

    def test_conflict_with_existing_target_rejected_and_atomic(self):
        s, (a, b, d, e) = self._source()
        t = self._project("T4")
        # a is F/CH1 abs@200 shifted by 800 -> 1000; put a colliding item
        self._add(t, abs_body(ms=1000, fixture="F", channel=1, target=1))
        n_before = len(self.client.get(f"/api/projects/{t}").json()["items"])
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [d, e], "landing_ms": 1000})
        self.assertEqual(r.status_code, 409)
        self.assertIn("同时起步", r.json()["detail"])
        n_after = len(self.client.get(f"/api/projects/{t}").json()["items"])
        self.assertEqual(n_before, n_after)
        # the whole batch rolls back (no partially inserted ancestors)
        self.assertEqual(n_after, 1)
        # source untouched as well
        self.assertEqual(len(self.client.get(f"/api/projects/{s}").json()["items"]), 4)

    def test_conflict_among_batch_and_target_merged_decision(self):
        # the batch alone is valid; it only collides against a target item,
        # so the decision is taken on the merged project, not the fragment
        s, (a, b, d, e) = self._source()
        t = self._project("T5")
        # e lands on 1007 on F/CH4
        self._add(t, abs_body(ms=1007, fixture="F", channel=4, target=9))
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [d, e], "landing_ms": 1000})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.client.get(f"/api/projects/{t}").json()["items"]), 1)

    def test_empty_selection_rejected(self):
        s, ids = self._source()
        t = self._project("T6")
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [], "landing_ms": 0})
        self.assertEqual(r.status_code, 422)

    def test_invalid_item_rejected(self):
        s, ids = self._source()
        t = self._project("T7")
        n_before = len(self.client.get(f"/api/projects/{t}").json()["items"])
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [9999], "landing_ms": 0})
        self.assertEqual(r.status_code, 400)
        self.assertIn("不存在", r.json()["detail"])
        self.assertEqual(len(self.client.get(f"/api/projects/{t}").json()["items"]),
                         n_before)

    def test_item_id_from_another_project_is_invalid_for_source(self):
        # item rowids come from one global sequence; an id that exists in
        # a different project must count as missing for this source
        s, (a, b, d, e) = self._source()
        other = self._project("Other")
        foreign = self._add(other, abs_body(ms=0, fixture="Q"))
        t = self._project("T7b")
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [foreign], "landing_ms": 0})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get(f"/api/projects/{t}").json()["items"], [])
        # the foreign item's own project is untouched
        self.assertEqual(len(self.client.get(f"/api/projects/{other}").json()["items"]), 1)

    def test_repeated_transplants_into_same_target(self):
        s, (a, b, d, e) = self._source()
        t = self._project("T7c")
        r1 = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [a], "landing_ms": 0})
        self.assertEqual(r1.status_code, 201)
        r2 = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [a], "landing_ms": 500})
        self.assertEqual(r2.status_code, 201)
        starts = sorted(i["actual_start"] for i in r2.json()["items"])
        self.assertEqual(starts, [0, 500])
        # a third copy colliding with the first is rejected, atomic
        r3 = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [a], "landing_ms": 0})
        self.assertEqual(r3.status_code, 409)
        self.assertEqual(len(self.client.get(f"/api/projects/{t}").json()["items"]), 2)

    def test_source_project_missing_404(self):
        t = self._project("T8")
        r = self.client.post("/api/projects/9999/transplant", json={
            "target_project_id": t, "item_ids": [1], "landing_ms": 0})
        self.assertEqual(r.status_code, 404)

    def test_target_project_missing_rejected(self):
        s, (a, b, d, e) = self._source()
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": 9999, "item_ids": [a], "landing_ms": 0})
        self.assertEqual(r.status_code, 400)
        self.assertIn("目标工程不存在", r.json()["detail"])

    def test_same_source_and_target_rejected(self):
        s, (a, b, d, e) = self._source()
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": s, "item_ids": [a], "landing_ms": 0})
        self.assertEqual(r.status_code, 400)
        self.assertIn("不能相同", r.json()["detail"])

    def test_negative_landing_rejected(self):
        s, (a, b, d, e) = self._source()
        t = self._project("T9")
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [a], "landing_ms": -1})
        self.assertEqual(r.status_code, 422)

    def test_duplicate_ids_in_selection_move_once(self):
        s, (a, b, d, e) = self._source()
        t = self._project("T10")
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [a, a, a], "landing_ms": 0})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(len(r.json()["id_map"]), 1)

    def test_eval_in_target_matches_after_transplant(self):
        s, (a, b, d, e) = self._source()
        t = self._project("T11")
        r = self.client.post(f"/api/projects/{s}/transplant", json={
            "target_project_id": t, "item_ids": [d, e], "landing_ms": 1000})
        by_old = {m["source_id"]: m["new_id"] for m in r.json()["id_map"]}
        ev = self.client.get(f"/api/projects/{t}/eval", params={"t": 1050}).json()
        ch1 = next(c for c in ev["channels"] if c["fixture"] == "F"
                   and c["channel"] == 1)
        # a: 0->100 over 100ms starting 1000; at 1050 it is 50
        self.assertEqual(ch1["raw"], "50")
        self.assertEqual(ch1["active_item_id"], by_old[a])

    def test_single_item_endpoints_unchanged(self):
        # the original create/update/delete/eval contract still behaves
        p = self._project("T12")
        i = self._add(p, abs_body(ms=10, duration=20, target=40))
        got = self.client.get(f"/api/projects/{p}").json()["items"][0]
        self.assertEqual((got["actual_start"], got["actual_end"]), (10, 30))
        r = self.client.put(f"/api/projects/{p}/items/{i}",
                            json=abs_body(ms=30, duration=20, target=40))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["items"][0]["actual_start"], 30)


if __name__ == "__main__":
    unittest.main()
