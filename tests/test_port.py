"""Cross-project fragment porting (POST /api/projects/{id}/port).

The selected items plus all their upstream references are copied as one
batch; the fragment's earliest actual start is aligned to the landing
point; references are rewired to the freshly allocated ids; same-instant
conflicts are judged against the merged target project; any rejection
leaves both projects untouched.
"""

import os
import tempfile
import unittest

os.environ["LIGHTS_DB"] = os.path.join(tempfile.mkdtemp(), "test_port.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


def abs_body(fixture="L1", channel=1, target=100, duration=1000, ms=0):
    return {"fixture": fixture, "channel": channel, "target": target,
            "duration": duration, "start_mode": "abs", "start_ms": ms}


def ref_body(ref_id, edge="start", offset=0, fixture="L1", channel=1,
             target=100, duration=1000):
    return {"fixture": fixture, "channel": channel, "target": target,
            "duration": duration, "start_mode": "ref",
            "ref_id": ref_id, "ref_edge": edge, "ref_offset": offset}


class PortTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = TestClient(app)

    def setUp(self):
        self.src = self.client.post("/api/projects", json={"name": "src"}).json()["id"]
        self.dst = self.client.post("/api/projects", json={"name": "dst"}).json()["id"]
        self.src_url = f"/api/projects/{self.src}"
        self.dst_url = f"/api/projects/{self.dst}"

    def add(self, pid, body):
        r = self.client.post(f"/api/projects/{pid}/items", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def port(self, item_ids, landing, target=None, source=None):
        return self.client.post(
            f"/api/projects/{source or self.src}/port",
            json={"target_project_id": target or self.dst,
                  "item_ids": item_ids, "landing_ms": landing})

    def get(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ------------------------------------------------------------------
    # happy path
    # ------------------------------------------------------------------

    def test_port_chain_aligns_earliest_and_rewires_refs(self):
        a = self.add(self.src, abs_body(ms=1000, duration=500, channel=1))
        id_a = a["items"][0]["id"]
        b = self.add(self.src, ref_body(id_a, "end", 100, channel=2))
        id_b = b["items"][-1]["id"]
        self.add(self.src, abs_body(ms=3000, channel=3))  # not selected

        before_src = self.get(self.src_url)
        r = self.port([id_b], 10000)
        self.assertEqual(r.status_code, 201, r.text)
        payload = r.json()

        # the response is the target project plus the old -> new id map
        port = payload["port"]
        self.assertEqual(port["source_project_id"], self.src)
        self.assertEqual(port["target_project_id"], self.dst)
        self.assertEqual(port["landing_ms"], 10000)
        id_map = {int(k): v for k, v in port["id_map"].items()}
        self.assertEqual(sorted(id_map), [id_a, id_b])  # upstream included
        self.assertEqual(sorted(port["new_item_ids"]), sorted(id_map.values()))
        self.assertIn(payload["focus_item_id"], port["new_item_ids"])

        items = {i["id"]: i for i in payload["items"]}
        new_a, new_b = items[id_map[id_a]], items[id_map[id_b]]
        # earliest actual start (1000) aligned to the landing point
        self.assertEqual(new_a["actual_start"], 10000)
        self.assertEqual(new_a["actual_end"], 10500)
        # relative spacing / duration / ref edge+offset semantics preserved
        self.assertEqual(new_b["actual_start"], 10600)
        self.assertEqual(new_b["duration"], 1000)
        self.assertEqual(new_b["start_mode"], "ref")
        self.assertEqual(new_b["ref_id"], new_a["id"])  # rewired, not id_a
        self.assertEqual(new_b["ref_edge"], "end")
        self.assertEqual(new_b["ref_offset"], 100)
        # target timeline shows exactly the two ported items
        self.assertEqual(len(items), 2)
        # source project untouched (nothing ported away, nothing edited)
        self.assertEqual(self.get(self.src_url), before_src)

    def test_shared_ancestor_ported_once(self):
        a = self.add(self.src, abs_body(ms=0, duration=100, channel=1))
        id_a = a["items"][0]["id"]
        b = self.add(self.src, ref_body(id_a, "end", 0, channel=2))
        id_b = b["items"][-1]["id"]
        c = self.add(self.src, ref_body(id_a, "start", 50, channel=3))
        id_c = c["items"][-1]["id"]

        r = self.port([id_b, id_c], 5000)
        self.assertEqual(r.status_code, 201, r.text)
        id_map = {int(k): v for k, v in r.json()["port"]["id_map"].items()}
        self.assertEqual(len(id_map), 3)  # shared ancestor only once
        items = {i["id"]: i for i in r.json()["items"]}
        self.assertEqual(items[id_map[id_b]]["ref_id"], id_map[id_a])
        self.assertEqual(items[id_map[id_c]]["ref_id"], id_map[id_a])
        self.assertEqual(items[id_map[id_a]]["actual_start"], 5000)
        self.assertEqual(items[id_map[id_b]]["actual_start"], 5100)
        self.assertEqual(items[id_map[id_c]]["actual_start"], 5050)

    def test_backward_shift_and_target_originals_unchanged(self):
        x = self.add(self.dst, abs_body(fixture="T", channel=9, ms=50))
        id_x = x["items"][0]["id"]
        before_dst = self.get(self.dst_url)

        a = self.add(self.src, abs_body(ms=2000, duration=300, channel=1))
        id_a = a["items"][0]["id"]
        b = self.add(self.src, abs_body(ms=2500, duration=0, channel=2))
        id_b = b["items"][-1]["id"]

        r = self.port([id_a, id_b], 0)  # shift = -2000
        self.assertEqual(r.status_code, 201, r.text)
        items = {i["id"]: i for i in r.json()["items"]}
        id_map = {int(k): v for k, v in r.json()["port"]["id_map"].items()}
        self.assertEqual(items[id_map[id_a]]["actual_start"], 0)
        self.assertEqual(items[id_map[id_b]]["actual_start"], 500)
        # the pre-existing target item is field-for-field unchanged
        before_x = next(i for i in before_dst["items"] if i["id"] == id_x)
        self.assertEqual(items[id_x], before_x)

    def test_port_into_empty_target_then_eval(self):
        a = self.add(self.src, abs_body(target=100, duration=1000, ms=100))
        id_a = a["items"][0]["id"]
        r = self.port([id_a], 1100)
        self.assertEqual(r.status_code, 201, r.text)
        new_id = r.json()["port"]["id_map"][str(id_a)]
        ev = self.client.get(f"{self.dst_url}/eval", params={"t": 1600}).json()
        ch = ev["channels"][0]
        self.assertEqual(ch["raw"], "50")
        self.assertEqual(ch["value"], 50)
        self.assertEqual(ch["active_item_id"], new_id)
        # source evaluation is untouched as well
        ev_src = self.client.get(f"{self.src_url}/eval", params={"t": 600}).json()
        self.assertEqual(ev_src["channels"][0]["raw"], "50")

    def test_deep_chain_insertion_order(self):
        # grandchildren first in the request: the not-yet-written ancestors
        # must still be allocated and rewired correctly
        a = self.add(self.src, abs_body(ms=0, duration=10, channel=1))
        id_a = a["items"][0]["id"]
        b = self.add(self.src, ref_body(id_a, "end", 5, channel=2))
        id_b = b["items"][-1]["id"]
        c = self.add(self.src, ref_body(id_b, "start", 5, channel=3))
        id_c = c["items"][-1]["id"]
        r = self.port([id_c, id_a], 100)  # selection order differs from chain
        self.assertEqual(r.status_code, 201, r.text)
        id_map = {int(k): v for k, v in r.json()["port"]["id_map"].items()}
        items = {i["id"]: i for i in r.json()["items"]}
        self.assertEqual(items[id_map[id_b]]["ref_id"], id_map[id_a])
        self.assertEqual(items[id_map[id_c]]["ref_id"], id_map[id_b])
        self.assertEqual(items[id_map[id_a]]["actual_start"], 100)
        self.assertEqual(items[id_map[id_b]]["actual_start"], 115)
        self.assertEqual(items[id_map[id_c]]["actual_start"], 120)

    # ------------------------------------------------------------------
    # rejections: both projects stay exactly as they were
    # ------------------------------------------------------------------

    def assert_unchanged(self, before_src, before_dst):
        self.assertEqual(self.get(self.src_url), before_src)
        self.assertEqual(self.get(self.dst_url), before_dst)

    def test_empty_selection_rejected(self):
        self.add(self.src, abs_body())
        before_src, before_dst = self.get(self.src_url), self.get(self.dst_url)
        r = self.port([], 0)
        self.assertEqual(r.status_code, 400)
        self.assertIn("未选择", r.json()["detail"])
        self.assert_unchanged(before_src, before_dst)

    def test_unknown_item_rejected(self):
        self.add(self.src, abs_body())
        before_src, before_dst = self.get(self.src_url), self.get(self.dst_url)
        r = self.port([999999], 0)
        self.assertEqual(r.status_code, 400)
        self.assertIn("不存在", r.json()["detail"])
        self.assert_unchanged(before_src, before_dst)

    def test_item_from_other_project_rejected(self):
        self.add(self.src, abs_body())
        other = self.add(self.dst, abs_body(fixture="T"))
        id_other = other["items"][0]["id"]
        before_src, before_dst = self.get(self.src_url), self.get(self.dst_url)
        r = self.port([id_other], 0)  # belongs to the target, not the source
        self.assertEqual(r.status_code, 400)
        self.assert_unchanged(before_src, before_dst)

    def test_same_project_rejected(self):
        a = self.add(self.src, abs_body())
        id_a = a["items"][0]["id"]
        before_src, before_dst = self.get(self.src_url), self.get(self.dst_url)
        r = self.port([id_a], 0, target=self.src)
        self.assertEqual(r.status_code, 400)
        self.assertIn("不能相同", r.json()["detail"])
        self.assert_unchanged(before_src, before_dst)

    def test_missing_projects_rejected(self):
        a = self.add(self.src, abs_body())
        id_a = a["items"][0]["id"]
        before_src, before_dst = self.get(self.src_url), self.get(self.dst_url)
        r = self.port([id_a], 0, target=999999)
        self.assertEqual(r.status_code, 404)
        r = self.port([id_a], 0, source=999999)
        self.assertEqual(r.status_code, 404)
        self.assert_unchanged(before_src, before_dst)

    def test_bad_landing_rejected(self):
        a = self.add(self.src, abs_body())
        id_a = a["items"][0]["id"]
        before_src, before_dst = self.get(self.src_url), self.get(self.dst_url)
        for bad in (-1, 1.5, "abc"):
            r = self.port([id_a], bad)
            self.assertEqual(r.status_code, 422, bad)
        self.assert_unchanged(before_src, before_dst)

    def test_same_instant_conflict_rolls_back_whole_batch(self):
        # target already has an item starting where a ported item would land
        self.add(self.dst, abs_body(fixture="L1", channel=1, ms=5000))
        a = self.add(self.src, abs_body(ms=0, duration=100, channel=1))
        id_a = a["items"][0]["id"]
        b = self.add(self.src, ref_body(id_a, "end", 0, channel=2))
        id_b = b["items"][-1]["id"]
        before_src, before_dst = self.get(self.src_url), self.get(self.dst_url)
        r = self.port([id_b], 5000)  # ported ancestor would start at 5000
        self.assertEqual(r.status_code, 400)
        self.assertIn("同时起步", r.json()["detail"])
        # the whole batch is rolled back: no partial items in the target
        self.assert_unchanged(before_src, before_dst)

    def test_conflict_with_ported_item_itself_impossible_but_merged_checked(self):
        # two ported items keep their source spacing (source was valid), so
        # the only possible collision is against the target's own items —
        # verify a collision on the *second* ported item also rolls back
        self.add(self.dst, abs_body(fixture="L1", channel=2, ms=5100))
        a = self.add(self.src, abs_body(ms=0, duration=100, channel=1))
        id_a = a["items"][0]["id"]
        b = self.add(self.src, ref_body(id_a, "end", 0, channel=2))
        id_b = b["items"][-1]["id"]
        before_src, before_dst = self.get(self.src_url), self.get(self.dst_url)
        r = self.port([id_b], 5000)  # second item lands at 5100 on CH2
        self.assertEqual(r.status_code, 400)
        self.assertIn("同时起步", r.json()["detail"])
        self.assert_unchanged(before_src, before_dst)

    def test_existing_single_item_endpoints_still_behave(self):
        # a port followed by ordinary edits keeps the old behaviour
        a = self.add(self.src, abs_body(ms=0, duration=100, channel=1))
        id_a = a["items"][0]["id"]
        r = self.port([id_a], 1000)
        self.assertEqual(r.status_code, 201, r.text)
        new_id = r.json()["port"]["id_map"][str(id_a)]
        # edit the ported item like any other item of the target project
        r = self.client.put(f"{self.dst_url}/items/{new_id}",
                            json=abs_body(ms=2000, duration=50))
        self.assertEqual(r.status_code, 200, r.text)
        item = r.json()["items"][0]
        self.assertEqual(item["actual_start"], 2000)
        self.assertEqual(item["actual_end"], 2050)
        # deleting a ported item that is referenced is still a 409
        b = self.add(self.src, ref_body(id_a, "end", 0, channel=2))
        id_b = b["items"][-1]["id"]
        r = self.port([id_b], 5000)
        self.assertEqual(r.status_code, 201, r.text)
        id_map = {int(k): v for k, v in r.json()["port"]["id_map"].items()}
        r = self.client.delete(f"{self.dst_url}/items/{id_map[id_a]}")
        self.assertEqual(r.status_code, 409)


if __name__ == "__main__":
    unittest.main()
