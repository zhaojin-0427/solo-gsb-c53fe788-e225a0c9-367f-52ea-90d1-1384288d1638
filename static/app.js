/* 灯光渐变时间轴 — 原生 JS 前端
 * 播放头只是一个时间值（任意非负小数毫秒，全程不做取整）；所有读数
 * 始终来自后端 /eval 精确求值，因此拖动 / 跳转 / 暂停后的读数与直接
 * 对同一时刻求值完全一致。
 */
"use strict";

const state = {
  projectId: null,
  project: null,          // GET /api/projects/:id
  projects: [],           // GET /api/projects (list, for the porter panel)
  readouts: null,         // GET /api/projects/:id/eval
  time: 0,                // 播放头（ms，播放时可为小数）
  playing: false,
  rafId: null,
  lastFrameTs: null,
  selectedId: null,
  lanes: [],              // [{key, fixture, channel, items:[...sorted]}]
  laneKeys: [],
  pxPerMs: 0.08,
  headPad: 20,
  laneH: 46,
  labelW: 110,
  timeMax: 1000,
  evalInflight: false,
  // Bumped on every project-data change (project switch / successful
  // create-update-delete).  An eval result is reusable only while both the
  // project and this generation match the request it came from — the same
  // playhead instant in a different project or after an edit needs a fresh
  // /eval, never a cached readout taken from the old data.
  evalGen: 0,
  // Monotonic ticket handed out by openProject: only the call holding the
  // latest ticket may apply its detail response.  Comparing project ids
  // cannot spot a stale detail request — in A→B→A the first A response
  // carries the same id as the live request — but this sequence number can.
  openSeq: 0,
  // cache key of the readouts currently shown: `${projectId}|${gen}|${time}`;
  // null means no readout may be reused yet (e.g. current eval failed)
  readoutKey: null,
  // The exact playhead instant the on-screen readouts were evaluated for.
  // A readout is trustworthy only while BOTH the project actually shown
  // (state.project, not the pending selection state.projectId) is the same
  // AND the playhead has not moved: while a detail request is in flight the
  // playhead may jump to another instant, requestEval is then suspended, and
  // the old instant's readout must be withdrawn at once rather than left
  // beside the new playhead position.
  readoutTime: null,
  dragging: false,
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const kid of kids) {
    if (kid == null) continue;
    node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
  }
  return node;
};

const SVGNS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  return node;
};

function fmtTime(ms) {
  ms = Math.max(0, ms);
  if (ms < 1000) {
    // keep up to 3 fractional digits so a decimal playhead is not hidden
    const txt = ms.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
    return `${txt} ms`;
  }
  return `${(ms / 1000).toFixed(3)} s`;
}

function toast(msg) {
  const t = $("#errorToast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._tid);
  toast._tid = setTimeout(() => t.classList.add("hidden"), 3200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : `请求失败 (${res.status})`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

// ---------------------------------------------------------------------------
// Project loading
// ---------------------------------------------------------------------------

async function loadProjects(selectId) {
  const projects = await api("/api/projects");
  state.projects = projects;
  const sel = $("#projectSelect");
  sel.innerHTML = "";
  for (const p of projects) {
    sel.appendChild(el("option", { value: p.id },
      `#${p.id} ${p.name}（${p.item_count} 项）`));
  }
  const chosen = selectId || Number(localStorage.getItem("lightsProject"));
  if (chosen && projects.some((p) => p.id === chosen)) {
    sel.value = String(chosen);
  }
  if (!projects.length) {
    // no project yet: create one so the UI always has something to show
    const p = await api("/api/projects", { method: "POST", body: JSON.stringify({ name: "未命名工程" }) });
    return loadProjects(p.id);
  }
  state.projectId = Number(sel.value);
  localStorage.setItem("lightsProject", state.projectId);
}

async function openProject(id) {
  // Take the next detail-request ticket: only the call still holding the
  // latest ticket is valid and may apply its response.  A project-id
  // comparison is not enough — in A→B→A the first A request carries the
  // same id as the live one, yet its older response must be discarded.
  const seq = ++state.openSeq;
  // invalidate any eval belonging to the previously open project: a
  // response still in flight must be discarded rather than shown here
  state.projectId = id;
  state.evalGen += 1;
  localStorage.setItem("lightsProject", id);
  let payload;
  try {
    payload = await api(`/api/projects/${id}`);
  } catch (e) {
    // a superseded request — success or failure — must leave the current
    // page untouched: the user has already moved on to another project
    if (seq !== state.openSeq) return;
    // The latest selection failed to load.  Roll the context back to the
    // project still on screen so the lanes, the editor, the selector and
    // every later eval keep describing the same project — the failure
    // must not leave mismatched readouts behind — then surface the error.
    // Do NOT restore evalGen: the generation was bumped when the switch
    // started, which already invalidated every cached / in-flight eval
    // (including old-instant responses arriving late); restoring it would
    // let a stale readout be mistaken for one of the rolled-back context.
    state.projectId = state.project ? state.project.id : null;
    if (state.projectId == null) {
      localStorage.removeItem("lightsProject");
    } else {
      localStorage.setItem("lightsProject", state.projectId);
      $("#projectSelect").value = String(state.projectId);
    }
    toast(e.message);
    // The playhead may have moved while the failed detail was in flight
    // (e.g. A shown at t0, B selected, head jumped to t1, B 404s).  Any
    // readout left on screen is the old project's OLD-instant value;
    // withdraw it and evaluate the restored project at the live instant,
    // so the settled readout is A@t1 — never A@t0 beside the t1 head.
    withdrawStaleReadout();
    await requestEval(true);
    return;
  }
  // a newer selection superseded this request while it was in flight:
  // discard the late response instead of rebuilding the lanes with it
  if (seq !== state.openSeq) return;
  state.project = payload;
  state.selectedId = null;
  buildLanes();
  // clear readouts from the old project before re-evaluating so switching
  // to an empty project leaves no stale channels on screen
  state.readouts = null;
  state.readoutKey = null;
  state.readoutTime = null;
  renderReadouts();
  renderTimeline();
  renderEditor();
  renderPorter();
  // evaluate the live playhead instant against the project just applied:
  // eval requests made while the detail was in flight were held back (see
  // requestEval), so this is what settles the readouts on the current
  // instant of the last-selected project
  await requestEval(true);
}

function buildLanes() {
  const map = new Map();
  for (const it of state.project.items) {
    const key = `${it.fixture}|${it.channel}`;
    if (!map.has(key)) map.set(key, { key, fixture: it.fixture, channel: it.channel, items: [] });
    map.get(key).items.push(it);
  }
  state.lanes = [...map.values()].sort((a, b) =>
    a.fixture.localeCompare(b.fixture, "zh") || a.channel - b.channel);
  state.laneKeys = state.lanes.map((l) => l.key);
  for (const lane of state.lanes) {
    lane.items.sort((a, b) => a.actual_start - b.actual_start || a.id - b.id);
  }
  const maxEnd = state.project.duration || 0;
  state.timeMax = Math.max(1000, Math.ceil(maxEnd * 1.15));
  // adaptive zoom: fit ~80% width but keep a sane range
  const viewW = $("#timelineScroll").clientWidth - state.labelW - state.headPad * 2;
  if (viewW > 200) {
    state.pxPerMs = Math.min(0.25, Math.max(0.008, viewW / Math.max(state.timeMax, 1)));
  }
}

// ---------------------------------------------------------------------------
// Timeline (SVG)
// ---------------------------------------------------------------------------

function niceStep(span) {
  const targetPx = 110 / state.pxPerMs; // ~110px between ticks
  const pow = Math.pow(10, Math.floor(Math.log10(targetPx)));
  for (const m of [1, 2, 5, 10, 20, 50]) {
    if (m * pow >= targetPx) return m * pow;
  }
  return 100 * pow;
}

function renderTimeline() {
  const host = $("#timeline");
  host.innerHTML = "";

  const w = state.labelW + state.headPad * 2 + state.timeMax * state.pxPerMs;
  const axisH = 28;
  const h = axisH + state.lanes.length * state.laneH + 8;
  const svg = svgEl("svg", { class: "tl", width: w, height: h, style: `min-width:${w}px` });

  // gradient defs
  const defs = svgEl("defs");
  const grad = svgEl("linearGradient", { id: "fadeGrad", x1: 0, y1: 0, x2: 1, y2: 0 });
  grad.appendChild(svgEl("stop", { offset: "0%", "stop-color": "rgba(240,180,41,0.35)" }));
  grad.appendChild(svgEl("stop", { offset: "100%", "stop-color": "#f0b429" }));
  defs.appendChild(grad);
  svg.appendChild(defs);

  const xAt = (ms) => state.labelW + state.headPad + ms * state.pxPerMs;

  // transparent drag layer below the item groups: item clicks still reach
  // the item groups because they are inserted after this element
  svg.appendChild(svgEl("rect", {
    x: 0, y: 0, width: w, height: h, class: "drag-overlay",
    onmousedown: startDrag,
  }));

  // grid + ticks
  const step = niceStep(state.timeMax);
  for (let t = 0; t <= state.timeMax + 1; t += step) {
    const x = xAt(t);
    svg.appendChild(svgEl("line", {
      x1: x, y1: 0, x2: x, y2: h, class: "grid-line",
      "stroke-dasharray": t === 0 ? "none" : "2 4",
    }));
    const lab = svgEl("text", { x: x + 3, y: 14, class: "tick-label" });
    lab.textContent = t < 1000 ? `${t}` : `${t / 1000}s`;
    svg.appendChild(lab);
  }

  // lanes
  state.lanes.forEach((lane, li) => {
    const y = axisH + li * state.laneH;
    svg.appendChild(svgEl("rect", {
      x: 0, y, width: w, height: state.laneH,
      class: `lane-bg ${li % 2 ? "alt" : ""}`,
    }));
    const name = svgEl("text", { x: 8, y: y + state.laneH / 2 + 4, class: "lane-label" });
    name.textContent = `${lane.fixture} · CH${lane.channel}`;
    svg.appendChild(name);

    for (const it of lane.items) {
      const x = xAt(it.actual_start);
      const classes = ["item-block"];
      if (it.id === state.selectedId) classes.push("selected");
      if (isInChain(it.id)) classes.push("chain");
      if (isPorted(it.id)) classes.push("ported");
      const g = svgEl("g", {
        class: classes.join(" "),
        onclick: () => selectItem(it.id),
      });
      if (it.duration === 0) {
        g.appendChild(svgEl("rect", {
          x: x - 3, y: y + 6, width: 6, height: state.laneH - 12, rx: 2,
          class: "item-rect instant",
        }));
      } else {
        const rw = Math.max(3, it.duration * state.pxPerMs);
        const interrupted = it.interrupted_by != null;
        g.appendChild(svgEl("rect", {
          x, y: y + 6, width: rw, height: state.laneH - 12, rx: 4,
          class: `item-rect${interrupted ? " interrupted" : ""}`,
        }));
        if (interrupted) {
          // diagonal red hatch over the portion after the interrupt point
          const by = state.project.items.find((q) => q.id === it.interrupted_by);
          if (by) {
            const hx = xAt(by.actual_start);
            g.appendChild(svgEl("rect", {
              x: hx, y: y + 6,
              width: Math.max(0, x + rw - hx),
              height: state.laneH - 12,
              fill: "rgba(229,72,77,0.35)",
            }));
          }
        }
        const label = svgEl("text", {
          x: x + 6, y: y + state.laneH / 2 + 3,
          class: "item-label" + (rw < 70 ? " on-dark" : ""),
        });
        label.textContent = rw > 40 ? `${it.target}` : "";
        g.appendChild(label);
      }
      if (it.start_mode === "ref") {
        const mk = svgEl("text", {
          x: x + (it.duration === 0 ? 6 : Math.max(4, it.duration * state.pxPerMs) - 12),
          y: y + 16, class: "ref-mark" + (it.duration !== 0 ? "" : " on-dark"),
        });
        mk.textContent = "↩";
        g.appendChild(mk);
      }
      svg.appendChild(g);
    }
  });

  // playhead (never blocks mouse events)
  const px = xAt(Math.min(state.time, state.timeMax));
  const head = svgEl("g", { id: "playhead", style: "pointer-events:none" });
  head.appendChild(svgEl("polygon", {
    points: `${px - 5},0 ${px + 5},0 ${px},8`, class: "playhead-head",
  }));
  head.appendChild(svgEl("line", {
    x1: px, y1: 8, x2: px, y2: h, class: "playhead-line",
  }));
  svg.appendChild(head);

  host.appendChild(svg);
}

function isInChain(id) {
  const sel = currentSelected();
  if (!sel) return false;
  if (sel.id === id) return false;
  return sel.referrers.includes(id) || refAncestors(sel).includes(id);
}

function refAncestors(item) {
  const out = [];
  let cur = item;
  const seen = new Set();
  while (cur && cur.start_mode === "ref" && !seen.has(cur.ref_id)) {
    seen.add(cur.ref_id);
    out.push(cur.ref_id);
    cur = state.project.items.find((q) => q.id === cur.ref_id);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Dragging the playhead
// ---------------------------------------------------------------------------

function timeFromEvent(evt) {
  const svg = document.querySelector("svg.tl");
  const rect = svg.getBoundingClientRect();
  const x = evt.clientX - rect.left;
  const ms = (x - state.labelW - state.headPad) / state.pxPerMs;
  return Math.max(0, ms);
}

function startDrag(evt) {
  state.dragging = true;
  seekTo(timeFromEvent(evt));
  const move = (e) => { if (state.dragging) seekTo(timeFromEvent(e)); };
  const up = () => {
    state.dragging = false;
    window.removeEventListener("mousemove", move);
    window.removeEventListener("mouseup", up);
    // drag end: re-send the exact current instant so the readout settles
    // on it even if the final move's request was skipped or throttled
    requestEval(true);
  };
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", up);
  evt.preventDefault();
}

// ---------------------------------------------------------------------------
// Playback
// ---------------------------------------------------------------------------

function setPlaying(on) {
  state.playing = on;
  $("#playBtn").textContent = on ? "⏸ 暂停" : "▶ 播放";
  $("#playBtn").classList.toggle("primary", !on);
  if (on) {
    state.lastFrameTs = null;
    cancelAnimationFrame(state.rafId);
    const tick = (ts) => {
      if (!state.playing) return;
      if (state.lastFrameTs == null) state.lastFrameTs = ts;
      const dt = ts - state.lastFrameTs;
      state.lastFrameTs = ts;
      state.time += dt; // wall-clock ms == timeline ms (fractional kept)
      updateClockAndHead();
      requestEval(false);
      if (state.time > state.timeMax) {
        setPlaying(false);
        return;
      }
      state.rafId = requestAnimationFrame(tick);
    };
    state.rafId = requestAnimationFrame(tick);
  } else {
    cancelAnimationFrame(state.rafId);
    // settle on the exact current instant: throttle may have skipped the
    // last frame, so guarantee the readout matches direct eval at this time
    requestEval(true);
  }
}

function seekTo(t) {
  state.time = Math.max(0, t);
  updateClockAndHead();
  requestEval(true);
}

function updateClockAndHead() {
  $("#clock").textContent = fmtTime(state.time);
  // echo the exact playhead position into the jump box, verbatim: no
  // rounding, so pressing Go re-seeks to precisely this instant
  $("#jumpInput").value = String(state.time);
  const svg = document.querySelector("svg.tl");
  if (!svg) return;
  const x = state.labelW + state.headPad +
        Math.min(state.time, state.timeMax) * state.pxPerMs;
  const head = svg.querySelector("#playhead");
  if (!head) return;
  const poly = head.querySelector("polygon");
  const line = head.querySelector("line");
  const h = svg.height.baseVal.value;
  poly.setAttribute("points", `${x - 5},0 ${x + 5},0 ${x},8`);
  line.setAttribute("x1", x);
  line.setAttribute("x2", x);
  line.setAttribute("y2", h);
}

async function requestEval(immediate) {
  // Only evaluate the project whose detail is actually on screen.  While a
  // switch is still in flight — or its detail request failed — projectId
  // already points at the next project, and evaluating it here would paint
  // its channels onto the lanes of the project still displayed.  The valid
  // detail response re-runs this for the live instant as soon as it lands.
  if (!state.project || state.project.id !== state.projectId) {
    // Eval is suspended, but the playhead keeps moving (drag / seek /
    // playback / jump).  The readout on screen belongs to the displayed
    // project at an EARLIER instant: it stays valid only while the head
    // sits at that same instant; once it moves, the old readout must be
    // withdrawn immediately instead of lingering beside the new head
    // position (where it would otherwise survive a failed switch that
    // rolls the context back).  Nothing can replace it until a detail is
    // applied — the landing / rollback path re-runs requestEval.
    withdrawStaleReadout();
    return;
  }
  // the playhead is an arbitrary non-negative (possibly fractional) ms
  // value; send it verbatim — no rounding anywhere in the chain, so the
  // readout always matches a direct /eval call for this exact instant
  const tq = state.time;
  const pid = state.projectId;
  const gen = state.evalGen;
  // Cache is reusable only for the exact same project + data generation +
  // instant.  Same time after a project switch or a successful edit bumps
  // the generation, so the old readout can never be served again.
  const key = `${pid}|${gen}|${tq}`;
  // one in-flight request at a time; remember to re-run afterwards (the
  // re-run re-reads project/gen/time so it always evaluates the current
  // data, even if it changed while the request was in flight).  This must
  // precede cache reuse: a same-key response still in flight may belong to
  // stale data, and the queued re-run is what replaces it.
  if (state.evalInflight) {
    state.evalQueued = true;
    return;
  }
  if (state.readoutKey === key) return;
  // during playback, cap request rate to ~12/s; seeks / pause are always
  // immediate.  A skipped frame schedules one trailing run so the readout
  // eventually settles on the exact current instant.
  const now = performance.now();
  if (!immediate && now - (state._lastEvalKick || -100) < 80) {
    if (state._evalTrailTimer == null) {
      state._evalTrailTimer = setTimeout(() => {
        state._evalTrailTimer = null;
        requestEval(true);
      }, 80);
    }
    return;
  }
  state._lastEvalKick = now;

  state.evalInflight = true;
  try {
    const data = await api(
      `/api/projects/${pid}/eval?t=${encodeURIComponent(String(tq))}`);
    // stale response guard: the project was switched, the data changed
    // OR the playhead moved to another instant while the request was in
    // flight.  Never overwrite the current view with an old-instant
    // readout; queue a re-run only if nothing already covers the current
    // instant, so the current data is evaluated without redundant
    // requests.
    if (pid !== state.projectId || gen !== state.evalGen ||
        tq !== state.time) {
      if (state.readoutKey !==
          `${state.projectId}|${state.evalGen}|${state.time}`) {
        state.evalQueued = true;
      }
      return;
    }
    state.readouts = data;
    state.readoutKey = key;
    state.readoutTime = tq;
    renderReadouts();
  } catch (e) {
    // only the request for the current project/data/instant may touch the
    // display; a stale failure must not wipe readouts belonging to a newer
    // context (a newer project, newer data, or a later playhead instant)
    if (pid === state.projectId && gen === state.evalGen &&
        tq === state.time) {
      // current project can't be evaluated: drop the stale readout (its
      // channels may no longer exist) and surface the error; readoutKey
      // stays null so a later retry is not deduped away
      state.readouts = null;
      state.readoutKey = null;
      state.readoutTime = null;
      renderReadouts();
      toast(e.message);
    }
    // A stale failure or success leaves the current instant unaccounted
    // for unless a newer eval of the live context is already in flight or
    // its result is cached; the shared finally block only re-runs when
    // something got queued, so ask it here.
    if ((pid !== state.projectId || gen !== state.evalGen ||
         tq !== state.time) &&
        !state.evalQueued &&
        state.readoutKey !==
        `${state.projectId}|${state.evalGen}|${state.time}`) {
      state.evalQueued = true;
    }
  } finally {
    state.evalInflight = false;
    if (state.evalQueued) {
      state.evalQueued = false;
      requestEval(true);
    }
  }
}

// ---------------------------------------------------------------------------
// Readouts
// ---------------------------------------------------------------------------

// A readout on screen is valid only for the project actually displayed
// (state.project) at the instant it was evaluated (state.readoutTime).
// Whenever that premise no longer holds — in practice: the playhead moved
// to another instant while a project switch was in flight and requestEval
// was therefore suspended — take the readout down immediately.  No toast
// and no replacement request here: the detail landing / failure-rollback
// path re-runs requestEval for the live instant.  Clearing readoutKey as
// well guarantees the withdrawn readout can never be served from cache.
function withdrawStaleReadout() {
  const stillValid = state.readouts != null &&
    state.readoutTime != null &&
    state.readoutTime === state.time;
  if (stillValid) return;
  if (state.readouts == null && state.readoutKey == null) return;
  state.readouts = null;
  state.readoutKey = null;
  state.readoutTime = null;
  renderReadouts();
}

function renderReadouts() {
  const host = $("#readouts");
  host.innerHTML = "";
  if (!state.readouts) return;
  const byKey = new Map(state.readouts.channels.map((c) =>
    [`${c.fixture}|${c.channel}`, c]));
  for (const lane of state.lanes) {
    const c = byKey.get(lane.key);
    if (!c) continue;
    const activeItem = c.active_item_id != null
      ? state.project.items.find((i) => i.id === c.active_item_id) : null;
    let state2 = "idle";
    if (activeItem) {
      const inFade = activeItem.duration > 0 &&
        state.time >= activeItem.actual_start &&
        state.time < activeItem.actual_end;
      state2 = inFade ? "fading" : "held";
    }
    const interrupter = activeItem && activeItem.interrupted_by != null
      ? itemById(activeItem.interrupted_by) : null;
    const interrupted = !!interrupter &&
      state.time < interrupter.actual_start &&
      state.time >= activeItem.actual_start;
    const card = el("div", { class: `readout ${state2}${interrupted ? " interrupt" : ""}` },
      el("div", { class: "fk" }, `${lane.fixture} · CH${lane.channel}`),
      el("div", { class: "val" }, String(c.value)),
      el("div", { class: "raw" }, `精确值 ${c.raw}`),
    );
    if (state2 === "fading") card.appendChild(el("span", { class: "badge" }, "渐变中"));
    if (interrupted) card.appendChild(el("span", { class: "badge" }, "渐变将被打断"));
    host.appendChild(card);
  }
}

function itemById(id) {
  return state.project.items.find((i) => i.id === id);
}

// ---------------------------------------------------------------------------
// Editor
// ---------------------------------------------------------------------------

function currentSelected() {
  return state.selectedId != null ? itemById(state.selectedId) : null;
}

function selectItem(id) {
  state.selectedId = id;
  renderTimeline();
  renderEditor();
}

function blankForm() {
  const fixtures = [...new Set(state.project.items.map((i) => i.fixture))];
  return {
    fixture: fixtures[0] || "面光1",
    channel: 1, target: 255, duration: 1000,
    start_mode: "abs", start_ms: 0,
    ref_id: state.project.items[0]?.id ?? null,
    ref_edge: "start", ref_offset: 0,
  };
}

function renderEditor() {
  const host = $("#editor");
  host.innerHTML = "";
  const it = currentSelected();
  $("#editorTitle").textContent = it ? `提示项 #${it.id}` : "新建提示项";

  if (it) {
    const parents = refAncestors(it);
    const meta = el("div", { class: "item-meta" });
    meta.innerHTML =
      `实际起步：<b>${it.actual_start} ms</b>　结束：<b>${it.actual_end} ms</b>`;
    if (it.interrupted_by != null) {
      meta.appendChild(el("div", { style: "color:#ff8a90;margin-top:4px" },
        `⚠ 渐变在结束前被 #${it.interrupted_by} 打断`));
    }
    if (it.start_mode === "ref") {
      meta.appendChild(el("div", { class: "chain-list" },
        `引用：#${it.ref_id} 的${it.ref_edge === "start" ? "开始" : "结束"} + ${it.ref_offset}ms`));
    }
    if (parents.length || it.referrers.length) {
      const chain = el("div", { class: "chain-list" });
      if (parents.length) {
        chain.appendChild(document.createTextNode("上游引用链："));
        parents.forEach((pid) =>
          chain.appendChild(el("span", { class: "chip parent", onclick: () => selectItem(pid) },
            `#${pid}`)));
      }
      if (it.referrers.length) {
        chain.appendChild(el("div", {}, "下游依赖（引用本项）："));
        it.referrers.forEach((cid) =>
          chain.appendChild(el("span", { class: "chip", onclick: () => selectItem(cid) },
            `#${cid}`)));
      }
      meta.appendChild(chain);
    }
    host.appendChild(meta);
  }
  host.appendChild(buildForm(it || blankForm(), !!it));
}

function buildForm(it, isEdit) {
  const form = el("div");
  const fixtures = [...new Set(state.project.items.map((i) => i.fixture))];
  if (!fixtures.includes(it.fixture) && it.fixture) fixtures.unshift(it.fixture);

  const fixtureRow = el("div", { class: "form-row" },
    el("label", {}, "灯具"),
    el("input", {
      type: "text", list: "fixtureList", value: it.fixture,
      "data-f": "fixture", placeholder: "如：面光1",
    }));
  const datalist = el("datalist", { id: "fixtureList" });
  fixtures.forEach((f) => datalist.appendChild(el("option", { value: f })));
  fixtureRow.appendChild(datalist);

  const grid = el("div", { class: "form-grid" },
    numField("通道 (1-512)", "channel", it.channel, 1, 512),
    numField("目标值 (0-255)", "target", it.target, 0, 255),
    numField("渐变时长 (ms)", "duration", it.duration, 0),
  );

  // mode switch (no item to reference until the project has at least one)
  const hasRefs = state.project.items.length > 0;
  const mode = el("div", { class: "mode-switch" },
    el("button", {
      type: "button", "data-mode": "abs",
      class: it.start_mode === "abs" ? "active" : "",
      onclick: () => switchMode(form, "abs"),
    }, "绝对时间"),
    el("button", {
      type: "button", "data-mode": "ref",
      class: it.start_mode === "ref" ? "active" : "",
      disabled: hasRefs ? null : "disabled",
      title: hasRefs ? "" : "工程中还没有可引用的提示项",
      onclick: () => { if (hasRefs) switchMode(form, "ref"); },
    }, "引用另一项"));

  const absBox = el("div", { class: "form-row", "data-box": "abs" },
    el("label", {}, "起步绝对时间 (ms)"),
    el("input", { type: "number", min: 0, step: 1, value: it.start_ms ?? 0, "data-f": "start_ms" }));

  const refSelect = el("select", { "data-f": "ref_id" });
  state.project.items.forEach((q) =>
    refSelect.appendChild(el("option", { value: q.id, selected: q.id === it.ref_id },
      `#${q.id} ${q.fixture} CH${q.channel} →${q.target}`)));
  const edgeSelect = el("select", { "data-f": "ref_edge" });
  edgeSelect.appendChild(el("option", { value: "start", selected: it.ref_edge !== "end" }, "开始"));
  edgeSelect.appendChild(el("option", { value: "end", selected: it.ref_edge === "end" }, "结束"));
  const refBox = el("div", { "data-box": "ref" },
    el("div", { class: "form-row" }, el("label", {}, "引用提示项"), refSelect),
    el("div", { class: "form-grid" },
      el("div", { class: "form-row" }, el("label", {}, "引用边"), edgeSelect),
      numField("偏移 (ms)", "ref_offset", it.ref_offset ?? 0, 0)));

  const errBox = el("div", { class: "form-error", style: "display:none" });

  const saveBtn = el("button", {
    class: "btn primary", type: "button",
    onclick: () => saveForm(form, isEdit, errBox),
  }, isEdit ? "保存修改" : "添加提示项");
  const actions = el("div", { class: "form-actions" }, saveBtn);
  if (isEdit) {
    actions.appendChild(el("button", {
      class: "btn danger", type: "button",
      onclick: () => deleteItem(it.id),
    }, "删除该项"));
  }
  actions.appendChild(el("button", {
    class: "btn", type: "button",
    onclick: () => { state.selectedId = null; renderTimeline(); renderEditor(); },
  }, "取消"));

  form.append(fixtureRow, grid, mode, absBox, refBox, errBox, actions);
  switchMode(form, it.start_mode);
  return form;
}

function numField(label, key, value, min, max) {
  const attrs = { type: "number", min, step: 1, value, "data-f": key };
  if (max != null) attrs.max = max;
  return el("div", { class: "form-row" }, el("label", {}, label), el("input", attrs));
}

function switchMode(form, mode) {
  form.querySelectorAll(".mode-switch button").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === mode));
  form.querySelector('[data-box="abs"]').style.display = mode === "abs" ? "" : "none";
  form.querySelector('[data-box="ref"]').style.display = mode === "ref" ? "" : "none";
}

function intField(form, key, label, { min, max } = {}) {
  const v = Number(form.querySelector(`[data-f="${key}"]`).value);
  if (!Number.isInteger(v) || (min != null && v < min) || (max != null && v > max)) {
    const range = max != null ? `（${min ?? "−∞"}～${max}）` : `（≥ ${min ?? 0}）`;
    throw new Error(`${label}必须是整数 ${range}`);
  }
  return v;
}

function collectForm(form) {
  const get = (f) => form.querySelector(`[data-f="${f}"]`);
  const mode = form.querySelector(".mode-switch button.active").dataset.mode;
  const fixture = get("fixture").value.trim();
  if (!fixture) throw new Error("灯具名称不能为空");
  const body = {
    fixture,
    channel: intField(form, "channel", "通道", { min: 1, max: 512 }),
    target: intField(form, "target", "目标值", { min: 0, max: 255 }),
    duration: intField(form, "duration", "渐变时长", { min: 0 }),
    start_mode: mode,
  };
  if (mode === "abs") {
    body.start_ms = intField(form, "start_ms", "起步绝对时间", { min: 0 });
  } else {
    const refSel = get("ref_id");
    if (!refSel || !refSel.options.length) throw new Error("工程中还没有可引用的提示项");
    body.ref_id = Number(refSel.value);
    body.ref_edge = get("ref_edge").value;
    body.ref_offset = intField(form, "ref_offset", "偏移", { min: 0 });
  }
  return body;
}

async function saveForm(form, isEdit, errBox) {
  let body;
  try {
    body = collectForm(form);
  } catch (e) {
    errBox.style.display = "";
    errBox.textContent = e.message;
    return;
  }
  try {
    const path = isEdit
      ? `/api/projects/${state.projectId}/items/${state.selectedId}`
      : `/api/projects/${state.projectId}/items`;
    const payload = await api(path, { method: isEdit ? "PUT" : "POST", body: JSON.stringify(body) });
    await applyProjectPayload(payload);
  } catch (e) {
    errBox.style.display = "";
    errBox.textContent = e.message;
  }
}

async function deleteItem(id) {
  if (!confirm(`确认删除提示项 #${id}？`)) return;
  try {
    const payload = await api(`/api/projects/${state.projectId}/items/${id}`,
      { method: "DELETE" });
    state.selectedId = null;
    await applyProjectPayload(payload);
  } catch (e) {
    toast(e.message);
  }
}

async function applyProjectPayload(payload) {
  state.project = payload;
  // the data changed: invalidate any cached / in-flight eval for the old
  // data so the same playhead instant is re-evaluated against this project
  state.evalGen += 1;
  const focus = payload.focus_item_id;
  state.selectedId = focus ?? state.selectedId;
  if (state.selectedId != null && !itemById(state.selectedId)) state.selectedId = null;
  buildLanes();
  // channels can disappear (e.g. deleting the last item of a fixture);
  // drop the old readout now so removed channels can't linger
  state.readouts = null;
  state.readoutKey = null;
  state.readoutTime = null;
  renderReadouts();
  renderTimeline();
  renderEditor();
  renderPorter();
  await requestEval(true);
  // refresh project selector counts
  await loadProjects(state.projectId);
}

// ---------------------------------------------------------------------------
// Cross-project fragment porting
// ---------------------------------------------------------------------------

// The checkbox selection belongs to the project on screen (the source).
// The last successful port's result stays visible — and keeps the ported
// items highlighted on the target timeline — until dismissed or replaced.
const portState = {
  selected: new Set(),  // source item ids checked in the porter panel
  targetId: null,       // last chosen target project id
  landing: null,        // raw landing input text; null -> target's duration
  result: null,         // {sourceId, targetId, targetName, landing, idMap, newIds}
  busy: false,          // a port request is in flight
};

function portClosureIds() {
  // selected items plus every upstream referenced item, as one batch — a
  // shared ancestor is collected once (mirrors the server-side closure)
  const byId = new Map(state.project.items.map((i) => [i.id, i]));
  const out = new Set();
  const stack = [...portState.selected];
  while (stack.length) {
    const cur = stack.pop();
    if (out.has(cur)) continue;
    out.add(cur);
    const it = byId.get(cur);
    if (it && it.start_mode === "ref" && it.ref_id != null) stack.push(it.ref_id);
  }
  return out;
}

function isPorted(id) {
  const r = portState.result;
  return !!(r && state.project && state.project.id === r.targetId &&
            r.newIds.includes(id));
}

function renderPorter() {
  const host = $("#porter");
  if (!host) return;
  host.innerHTML = "";
  if (!state.project) {
    host.appendChild(el("div", { class: "empty-note" }, "工程加载后可移植片段。"));
    return;
  }
  // drop checks for items that no longer exist (deleted / project switch)
  const alive = new Set(state.project.items.map((i) => i.id));
  portState.selected.forEach((id) => { if (!alive.has(id)) portState.selected.delete(id); });

  const list = el("div", { class: "port-items" });
  if (!state.project.items.length) {
    list.appendChild(el("div", { class: "empty-note" }, "当前工程没有提示项"));
  }
  for (const it of state.project.items) {
    const cb = el("input", { type: "checkbox" });
    cb.checked = portState.selected.has(it.id);
    cb.addEventListener("change", () => {
      if (cb.checked) portState.selected.add(it.id);
      else portState.selected.delete(it.id);
      renderPorter();
    });
    list.appendChild(el("label", { class: "port-item" }, cb,
      `#${it.id} ${it.fixture}·CH${it.channel} →${it.target} @${it.actual_start}ms`));
  }

  const closure = state.project.items.length ? portClosureIds() : new Set();
  const closureNote = el("div", { class: "port-closure" },
    portState.selected.size
      ? `已选 ${portState.selected.size} 项；连同全部上游引用共 ${closure.size} 项作为一批移植（共享祖先只移植一次）。`
      : "勾选若干提示项；其全部上游引用项会一并移植。");

  // target: any project except the one on screen
  const targets = (state.projects || []).filter((p) => p.id !== state.project.id);
  const targetSel = el("select", { id: "portTarget" });
  for (const p of targets) {
    targetSel.appendChild(el("option", {
      value: p.id, selected: p.id === portState.targetId,
    }, `#${p.id} ${p.name}（${p.item_count} 项）`));
  }
  if (!targets.length) {
    targetSel.appendChild(el("option", { value: "" }, "（请先用“＋ 新工程”创建目标工程）"));
  }
  const picked = targets.find((p) => p.id === portState.targetId) || targets[0] || null;
  portState.targetId = picked ? picked.id : null;
  targetSel.value = portState.targetId == null ? "" : String(portState.targetId);
  targetSel.addEventListener("change", () => {
    portState.targetId = Number(targetSel.value) || null;
    portState.landing = null;  // default landing follows the new target's end
    renderPorter();
  });

  const landingInput = el("input", {
    id: "portLanding", type: "number", min: 0, step: 1,
    value: portState.landing != null ? portState.landing : (picked ? picked.duration : 0),
  });
  landingInput.addEventListener("input", () => { portState.landing = landingInput.value; });

  const errBox = el("div", { class: "form-error", style: "display:none" });
  const submit = el("button", {
    class: "btn primary", type: "button",
    disabled: targets.length ? null : "disabled",
    onclick: () => submitPort(errBox),
  }, "移植到目标工程");

  host.append(
    el("div", { class: "form-row" },
      el("label", {}, `源工程：#${state.project.id} ${state.project.name} — 勾选要移植的提示项`),
      list),
    closureNote,
    el("div", { class: "form-row" }, el("label", {}, "目标工程"), targetSel),
    el("div", { class: "form-row" },
      el("label", {}, "落点（非负整数毫秒，片段最早实际起步对齐到此）"), landingInput),
    errBox,
    el("div", { class: "form-actions" }, submit),
  );

  const r = portState.result;
  if (r) {
    const box = el("div", { class: "port-result" });
    box.appendChild(el("div", {},
      `已移植到 #${r.targetId} ${r.targetName}（落点 ${r.landing}ms）——原 ID → 新 ID：`));
    const mapRow = el("div", { class: "port-map" });
    for (const [oldId, newId] of Object.entries(r.idMap)) {
      mapRow.appendChild(el("span", {
        class: "chip",
        title: "点击选中该移植项",
        onclick: () => {
          if (state.project && state.project.id === r.targetId) selectItem(newId);
        },
      }, `#${oldId} → #${newId}`));
    }
    box.appendChild(mapRow);
    box.appendChild(el("div", { class: "port-actions" },
      el("button", {
        class: "btn", type: "button",
        onclick: () => { portState.result = null; renderPorter(); renderTimeline(); },
      }, "清除移植标记")));
    host.appendChild(box);
  }
}

async function submitPort(errBox) {
  if (portState.busy) return;
  errBox.style.display = "none";
  const fail = (msg) => { errBox.style.display = ""; errBox.textContent = msg; };
  const ids = [...portState.selected];
  if (!ids.length) return fail("请至少勾选一个提示项");
  if (portState.targetId == null) return fail("请选择目标工程");
  const landing = Number($("#portLanding").value);
  if (!Number.isInteger(landing) || landing < 0) {
    return fail("落点必须是非负整数毫秒");
  }
  const sourceId = state.project.id;
  portState.busy = true;
  try {
    const payload = await api(`/api/projects/${sourceId}/port`, {
      method: "POST",
      body: JSON.stringify({
        target_project_id: portState.targetId,
        item_ids: ids,
        landing_ms: landing,
      }),
    });
    const port = payload.port;
    portState.result = {
      sourceId,
      targetId: port.target_project_id,
      targetName: payload.name,
      landing: port.landing_ms,
      idMap: port.id_map,
      newIds: port.new_item_ids,
    };
    portState.selected = new Set();
    // jump to the target project so its timeline shows the ported items
    // (green outline) together with the old -> new id mapping above
    await loadProjects(port.target_project_id);
    setPlaying(false);
    await openProject(port.target_project_id);
    state.selectedId = port.new_item_ids[0] ?? null;
    renderTimeline();
    renderEditor();
    renderPorter();
  } catch (e) {
    // rejected: both projects are unchanged on the server and the page
    // keeps showing the source project exactly as before
    fail(e.message);
  } finally {
    portState.busy = false;
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

async function init() {
  await loadProjects();
  await openProject(state.projectId);
  renderPorter();

  $("#projectSelect").addEventListener("change", async (e) => {
    setPlaying(false);
    await openProject(Number(e.target.value));
  });
  $("#newProjectBtn").addEventListener("click", async () => {
    const name = prompt("新工程名称", "未命名工程");
    if (!name || !name.trim()) return;
    try {
      const p = await api("/api/projects", { method: "POST", body: JSON.stringify({ name }) });
      await loadProjects(p.id);
      setPlaying(false);
      await openProject(p.id);
    } catch (e) { toast(e.message); }
  });
  $("#playBtn").addEventListener("click", () => {
    if (!state.playing && state.time >= state.timeMax) state.time = 0;
    setPlaying(!state.playing);
  });
  $("#resetBtn").addEventListener("click", () => { setPlaying(false); seekTo(0); });
  $("#jumpBtn").addEventListener("click", doJump);
  $("#jumpInput").addEventListener("keydown", (e) => { if (e.key === "Enter") doJump(); });

  window.addEventListener("resize", () => { buildLanes(); renderTimeline(); updateClockAndHead(); });
}

function doJump() {
  const t = Number($("#jumpInput").value);
  if (!Number.isFinite(t) || t < 0) { toast("请输入非负毫秒（可为小数）"); return; }
  setPlaying(false);
  seekTo(t);
}

init().catch((e) => toast(e.message));
