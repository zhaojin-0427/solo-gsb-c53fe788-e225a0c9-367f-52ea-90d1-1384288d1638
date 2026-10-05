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


if __name__ == "__main__":
    unittest.main()
