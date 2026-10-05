"""Static guards for the high-precision playhead chain in static/app.js.

The browser code has no JS test runner in this repo, so these tests pin
the exact regressions that were fixed:

* a playhead time produced by play / drag / jump must travel from
  internal state and the jump input to
  ``GET /api/projects/{id}/eval?t=`` **verbatim** — no fixed-decimal
  rounding (the old ``round6``) may reappear in the input echo, the
  dedup key, the throttle trailing re-send, or the request URL itself;
* eval results are cached only per (project, data generation, instant):
  switching projects or successfully creating / updating / deleting an
  item must bump the generation, drop cached readouts and force a fresh
  eval at the same playhead instant; a response that arrives after the
  context changed must be discarded and never overwrite the current
  view;
* switching to a project (including an empty one) clears the previous
  project's channels immediately, and a failed eval of the current
  project clears the stale readouts; a failed save must touch neither;
* rapid project switching is sequenced: every ``openProject`` call takes
  a monotonically increasing ticket and only the latest ticket may apply
  its ``GET /api/projects/{id}`` response (a project-id comparison cannot
  spot the stale request in A→B→A); a superseded detail response —
  success or failure — must leave the page untouched, while the latest
  selection's failure must surface a toast and roll the context back to
  the project still on screen so no mismatched readouts can be shown;
  and ``requestEval`` only evaluates the project whose detail is actually
  displayed, so an eval fired while a switch is in flight can never paint
  the next project's channels onto the previous project's lanes;
* while eval is thus suspended the playhead may still move (drag / seek /
  jump / playback): the displayed readout belongs to the on-screen project
  at an EARLIER instant, so it must be withdrawn as soon as the head leaves
  that instant — it is valid only for "project on screen + that instant" —
  and when the pending detail fails and the context rolls back, the
  restored project is re-evaluated at the live instant, so the settled
  readout is never the old instant's value parked beside the new head;
* an eval response arriving after the playhead moved is an old-instant
  response and must never be shown as the current readout (success nor
  failure); only a response matching project, data generation AND instant
  may touch the display, and every stale response arranges a fresh eval
  of the live instant.
"""

import re
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"


def function_body(src: str, signature: str) -> str:
    """Return the body of a top-level JS function (closing brace at col 0)."""
    m = re.search(re.escape(signature) + r"\s*\{.*?\n\}", src, re.S)
    if not m:
        raise AssertionError(f"{signature} not found in app.js")
    return m.group(0)


class TestPlayheadPrecisionChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP_JS.read_text(encoding="utf-8")
        cls.flat = re.sub(r"\s+", " ", cls.src)

    def test_no_fixed_decimal_rounding_helper(self):
        # the microsecond-rounding helper is gone from the whole chain
        self.assertNotIn("round6", self.src)

    def test_jump_input_echo_is_verbatim(self):
        # the jump box echoes the exact playhead, so Go re-seeks to it
        self.assertIn('$("#jumpInput").value = String(state.time);', self.flat)

    def test_eval_query_uses_verbatim_state_time(self):
        # the request time is the unrounded playhead value
        self.assertIn("const tq = state.time;", self.flat)
        self.assertIn("/eval?t=${encodeURIComponent(String(tq))}", self.src)

    def test_dedup_compares_verbatim_time(self):
        # the cache key embeds the same unrounded value that would be sent,
        # so the instant part of the key is still lossless
        self.assertIn('const key = `${pid}|${gen}|${tq}`;', self.src)
        self.assertIn("if (state.readoutKey === key) return;", self.flat)

    def test_drag_end_resends_current_instant(self):
        body = function_body(self.src, "function startDrag(evt)")
        up = body.split("const up = () => {", 1)
        self.assertEqual(len(up), 2, "mouseup handler not found")
        self.assertIn("requestEval(true)", up[1])

    def test_pause_and_seek_resend_immediately(self):
        # every seek evaluates at once
        seek = function_body(self.src, "function seekTo(t)")
        self.assertIn("requestEval(true)", seek)
        # pause settles on the exact current instant
        play = function_body(self.src, "function setPlaying(on)")
        else_branch = play.split("} else {", 1)[1]
        self.assertIn("requestEval(true)", else_branch)

    def test_playback_throttle_still_present(self):
        # playback frames go through the rate limiter (immediate=false) and
        # a skipped frame schedules one trailing re-send of the live time
        tick = function_body(self.src, "function setPlaying(on)")
        self.assertIn("requestEval(false)", tick)
        self.assertIn("_evalTrailTimer", self.src)
        trail = re.search(
            r"_evalTrailTimer = setTimeout\(\(\) => \{(.*?)\}, 80\)",
            self.src, re.S)
        self.assertIsNotNone(trail, "throttle trailing timer not found")
        self.assertIn("requestEval(true)", trail.group(1))


class TestEvalDataGeneration(unittest.TestCase):
    """Same playhead instant must be re-evaluated once the data changes."""

    @classmethod
    def setUpClass(cls):
        cls.src = APP_JS.read_text(encoding="utf-8")
        cls.flat = re.sub(r"\s+", " ", cls.src)
        cls.req = function_body(cls.src, "async function requestEval(immediate)")
        cls.open = function_body(cls.src, "async function openProject(id)")
        cls.apply = function_body(
            cls.src, "async function applyProjectPayload(payload)")

    def test_generation_state_exists(self):
        self.assertIn("evalGen: 0", self.flat)
        self.assertIn("readoutKey: null", self.flat)
        # the old time-only dedup bookkeeping is gone
        self.assertNotIn("lastEvalTime", self.src)

    def test_cache_key_includes_project_generation_and_time(self):
        self.assertIn("const pid = state.projectId;", self.flat)
        self.assertIn("const gen = state.evalGen;", self.flat)
        self.assertIn('const key = `${pid}|${gen}|${tq}`;', self.src)

    def test_project_switch_bumps_generation(self):
        self.assertIn("state.evalGen += 1;", self.open)

    def test_data_mutation_bumps_generation(self):
        self.assertIn("state.evalGen += 1;", self.apply)

    def test_switch_clears_old_readouts_before_request(self):
        # old channels (incl. empty target project) must not linger
        self.assertIn("state.readouts = null;", self.open)
        self.assertIn("state.readoutKey = null;", self.open)
        # scope to the success path (after the last stale-ticket guard);
        # the failure-rollback path legitimately re-evaluates too
        applied = self.open.rsplit("if (seq !== state.openSeq) return;", 1)[1]
        clear_at = applied.index("state.readouts = null;")
        req_at = applied.index("requestEval(true)")
        self.assertLess(clear_at, req_at)
        # lanes are rebuilt against the new project before the clear/render
        self.assertLess(applied.index("buildLanes();"), clear_at)

    def test_mutation_clears_old_readouts_before_request(self):
        self.assertIn("state.readouts = null;", self.apply)
        self.assertIn("state.readoutKey = null;", self.apply)
        clear_at = self.apply.index("state.readouts = null;")
        req_at = self.apply.index("requestEval(true)")
        self.assertLess(clear_at, req_at)

    def test_inflight_stale_success_is_discarded(self):
        # an in-flight response must be checked against the CURRENT context
        # AND the current playhead instant: a response for an earlier
        # instant must never become the readout beside the moved head
        self.assertIn(
            "if (pid !== state.projectId || gen !== state.evalGen ||\n"
            "        tq !== state.time)",
            self.req)
        # locate the stale-success block (first occurrence of the guard)
        head = self.req.split(
            "if (pid !== state.projectId || gen !== state.evalGen ||", 1)[1]
        block = head.split("return;", 1)[0]
        # ...must not overwrite state.readouts nor re-render...
        self.assertNotIn("state.readouts =", block)
        self.assertNotIn("renderReadouts()", block)
        # ...and must (conditionally) arrange a fresh eval of current data
        self.assertIn("state.evalQueued = true;", block)
        self.assertIn("state.readoutKey !==", block)
        # the current-instant key is rebuilt from live state
        self.assertIn(
            "`${state.projectId}|${state.evalGen}|${state.time}`", block)

    def test_inflight_old_instant_response_never_displayed(self):
        # the guard compares the request instant (tq) with the live head:
        # an old-instant response, success or failure, is treated as stale
        self.assertIn("tq !== state.time", self.req)
        # and it may be applied only when every part matches — the apply
        # block therefore sits AFTER the full three-part guard
        guard = self.req.index("tq !== state.time")
        applied = self.req.index("state.readouts = data;")
        self.assertLess(guard, applied)

    def test_inflight_stale_failure_ignored(self):
        # a stale request's failure must not wipe the newer context's
        # readouts nor toast its error; "stale" includes an old instant
        m = re.search(
            r"catch \(e\) \{\s*//.*?if \(pid === state\.projectId &&\s*"
            r"gen === state\.evalGen &&\s*tq === state\.time\) \{(.*?)\}\s*\}",
            self.req, re.S)
        self.assertIsNotNone(m, "context+instant-guarded catch block not found")
        live_block = m.group(1)
        # clearing/toasting only happens inside the current-context guard
        self.assertIn("state.readouts = null;", live_block)
        self.assertIn("state.readoutKey = null;", live_block)
        self.assertIn("renderReadouts();", live_block)
        self.assertIn("toast(e.message);", live_block)

    def test_stale_failure_arranges_rerun(self):
        # a stale failure must still leave a queued re-run so the current
        # data is evaluated (only when nothing in flight / cached covers it)
        self.assertIn(
            "(pid !== state.projectId || gen !== state.evalGen ||", self.req)
        self.assertIn("tq !== state.time)", self.req)
        self.assertIn("!state.evalQueued", self.req)
        # exactly three queue sites: single-flight, stale success, stale
        # failure — no other path may queue silently
        self.assertEqual(self.req.count("state.evalQueued = true;"), 3)

    def test_inflight_check_precedes_cache_reuse(self):
        # otherwise a same-key response in flight could be dedup-skipped and
        # its staleness would never trigger a compensating request
        inflight = self.req.index("if (state.evalInflight)")
        reuse = self.req.index("if (state.readoutKey === key) return;")
        self.assertLess(inflight, reuse)

    def test_queued_rerun_reads_current_context(self):
        # the single-flight re-run goes through requestEval again, which
        # re-reads projectId/evalGen/time (no captured stale values)
        tail = self.req.split("if (state.evalQueued) {", 1)[1]
        self.assertIn("requestEval(true);", tail)

    def test_failed_save_keeps_readouts(self):
        # saveForm's error path shows the form error and returns without
        # applying the payload / touching eval state (take the LAST catch)
        save = function_body(self.src, "async function saveForm(form, isEdit, errBox)")
        err = save.rsplit("} catch (e) {", 1)[1]
        self.assertNotIn("applyProjectPayload", err)
        self.assertNotIn("evalGen", err)
        self.assertNotIn("readouts = null", err)


class TestProjectDetailSequencing(unittest.TestCase):
    """Rapid project switching: only the latest detail request may apply.

    Switching projects before the previous GET /api/projects/{id} returned
    must never let a late response rebuild the lanes for a project the user
    has already left; the page may only ever show the last-selected project
    together with the readouts evaluated for it.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = APP_JS.read_text(encoding="utf-8")
        cls.flat = re.sub(r"\s+", " ", cls.src)
        cls.open = function_body(cls.src, "async function openProject(id)")
        cls.req = function_body(cls.src, "async function requestEval(immediate)")

    def test_open_sequence_state_exists(self):
        # a monotonic ticket lives next to the eval generation counter —
        # project id alone cannot identify the stale request in A→B→A
        self.assertIn("openSeq: 0", self.flat)

    def test_open_takes_ticket_before_request(self):
        self.assertIn("const seq = ++state.openSeq;", self.open)
        ticket = self.open.index("const seq = ++state.openSeq;")
        sent = self.open.index("await api(")
        self.assertLess(ticket, sent)

    def test_stale_detail_success_discarded_before_apply(self):
        # after the await the ticket is re-checked; a superseded response
        # returns without touching state.project or rebuilding the lanes
        sent = self.open.index("await api(")
        guard = self.open.index("if (seq !== state.openSeq) return;", sent)
        apply_at = self.open.index("state.project = payload;")
        self.assertLess(sent, guard)
        self.assertLess(guard, apply_at)
        self.assertLess(guard, self.open.index("buildLanes();"))

    def test_stale_detail_failure_leaves_page_untouched(self):
        # the catch must bail out on a superseded ticket BEFORE any state
        # mutation or toast: a stale failure changes nothing on screen
        catch = self.open.split("} catch (e) {", 1)[1]
        bail = catch.index("if (seq !== state.openSeq) return;")
        self.assertLess(bail, catch.index("toast("))
        stale_prefix = catch[:bail]
        self.assertNotIn("state.project", stale_prefix)
        self.assertNotIn("toast(", stale_prefix)

    def test_latest_detail_failure_toasts_and_restores_context(self):
        # the latest selection's failure must surface the error and roll
        # the context back to the project still on screen, so the selector,
        # the lanes and every later eval keep describing the same project
        # and no mismatched readouts can appear
        catch = self.open.split("} catch (e) {", 1)[1]
        self.assertIn("toast(e.message);", catch)
        self.assertIn("state.projectId = state.project", catch)
        # the selector follows the rollback so re-selecting the failed
        # project fires `change` again
        self.assertIn('$("#projectSelect").value', catch)

    def test_eval_only_for_displayed_project(self):
        # while a switch is in flight (or its load failed) projectId points
        # at a project whose detail is not on screen: never evaluate it,
        # its channels would land on the previous project's lanes
        guard = self.req.index("state.project.id !== state.projectId")
        self.assertLess(guard, self.req.index("const tq = state.time;"))

    def test_readout_instant_state_exists(self):
        # the on-screen readout carries the exact instant it was computed
        # for, so validity during a suspended switch can be judged from the
        # displayed project + playhead instant
        self.assertIn("readoutTime: null", self.flat)
        # recorded on apply together with the cache key
        apply_at = self.req.index("state.readoutKey = key;")
        self.assertIn("state.readoutTime = tq;", self.req)
        self.assertLess(apply_at, self.req.index("state.readoutTime = tq;"))
        # and dropped at every place the readout itself is dropped:
        # current-context eval failure, detail applied, mutation applied
        self.assertEqual(self.src.count("state.readoutTime = null;"), 4)

    def test_suspended_eval_withdraws_old_instant_readout(self):
        # the suspended branch (detail in flight / failed) must not just
        # return: when the playhead has left the readout's instant the old
        # readout is withdrawn immediately so it cannot sit beside the new
        # head position (and survive a rollback)
        guard = self.req.index("if (!state.project || state.project.id !== state.projectId)")
        head = self.req[guard:]
        body = head.split("{", 1)[1].split("return;", 1)[0]
        self.assertIn("withdrawStaleReadout();", body)

    def test_readout_valid_only_for_displayed_project_and_instant(self):
        # the withdrawal helper keeps the readout only while the on-screen
        # project's readout instant still equals the live playhead time
        helper = function_body(self.src, "function withdrawStaleReadout()")
        self.assertIn("state.readoutTime === state.time", helper)
        # and clears readouts / key / instant together, then re-renders
        clear = helper.split("state.readoutTime === state.time", 1)[1]
        self.assertIn("state.readouts = null;", clear)
        self.assertIn("state.readoutKey = null;", clear)
        self.assertIn("state.readoutTime = null;", clear)
        self.assertIn("renderReadouts();", clear)

    def test_latest_detail_failure_reevaluates_live_instant(self):
        # after rolling back to the on-screen project, the failure path
        # withdraws an old-instant readout and re-evaluates the restored
        # project at the CURRENT playhead (A@t1, not A@t0)
        catch = self.open.split("} catch (e) {", 1)[1]
        restore = catch.index("state.projectId = state.project")
        withdraw = catch.index("withdrawStaleReadout();")
        reeval = catch.index("requestEval(true);")
        toast_at = catch.index("toast(e.message);")
        self.assertLess(restore, reeval)
        self.assertLess(toast_at, reeval)
        self.assertLess(withdraw, reeval)

    def test_latest_detail_failure_does_not_restore_generation(self):
        # restoring evalGen would let a pre-switch cached/in-flight readout
        # pass the freshness checks of the rolled-back context
        catch = self.open.split("} catch (e) {", 1)[1]
        self.assertNotIn("evalGen -= 1", catch)
        self.assertNotIn("evalGen =", catch)

    def test_valid_detail_still_triggers_live_eval(self):
        # the applied detail is what settles the readouts on the current
        # instant of the last-selected project (scope to the success path
        # after the last stale-ticket guard)
        applied = self.open.rsplit("if (seq !== state.openSeq) return;", 1)[1]
        apply_at = applied.index("state.project = payload;")
        eval_at = applied.index("await requestEval(true);")
        self.assertLess(apply_at, eval_at)


if __name__ == "__main__":
    unittest.main()
