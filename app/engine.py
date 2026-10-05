"""Pure timing / value engine for the lighting fade timeline.

An item describes a target value for one fixture channel.  Its start is
either an absolute time (ms) or a reference to another item's start/end
plus a non-negative offset.  The engine

* validates references (missing / cycles) and rejects two items of the
  same fixture/channel starting at the same resolved instant,
* resolves every item to an absolute start time in reference order,
* builds per-channel piecewise-linear fade segments, each fade starting
  from the *unrounded* value left by the previous fade (a later item can
  interrupt an earlier fade),
* evaluates the channel values at any non-negative time, including
  fractional milliseconds (``fractions.Fraction``), exactly; display
  values round half up.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Tuple


class EngineError(Exception):
    """A validation failure. ``detail`` carries a Chinese user message."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Item:
    id: Optional[int]
    fixture: str
    channel: int
    target: int
    duration: int
    start_mode: str  # "abs" | "ref"
    start_ms: int | None = None
    ref_id: int | None = None
    ref_edge: str | None = None  # "start" | "end"
    ref_offset: int | None = None


@dataclass
class Segment:
    begin: int  # segment occupies [begin, end) (instant items: begin == end)
    end: int
    start_value: Fraction
    end_value: Fraction
    instant: bool
    item_id: int


@dataclass
class ResolvedItem:
    item: Item
    start: int
    end: int
    interrupted_by: Optional[int]


@dataclass
class ResolvedView:
    items: List[ResolvedItem]  # reference order, then id
    lanes: Dict[str, List[Segment]]  # key "fixture|channel", start order


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate(items: Iterable[Item]) -> None:
    items = list(items)
    by_id = {it.id: it for it in items if it.id is not None}

    # 1. missing references
    for it in items:
        if it.start_mode == "ref":
            if it.ref_id not in by_id:
                raise EngineError(f"提示项 {it.ref_id} 不存在，引用缺失")

    # 2. reference cycles
    _check_cycles(items, by_id)

    # 3. two items of the same fixture/channel starting at the same instant
    starts = resolve_starts(items, by_id)
    starts_at: Dict[Tuple[str, int], set] = {}
    for it in items:
        key = (it.fixture, it.channel)
        s = starts[it]
        bucket = starts_at.setdefault(key, set())
        if s in bucket:
            raise EngineError(
                f"灯具 {it.fixture} 通道 {it.channel} 在 {s}ms 已有一项同时起步"
            )
        bucket.add(s)


def _check_cycles(items: List[Item], by_id: Dict[int, Item]) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {it.id: WHITE for it in items if it.id is not None}

    for root in color:
        if color[root] != WHITE:
            continue
        # stack entries: (node, child iterator)
        stack: List[Tuple[int, Iterable[int]]] = [
            (root, iter([by_id[root].ref_id]) if by_id[root].start_mode == "ref" else iter(()))
        ]
        color[root] = GRAY
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                advanced = True
                if color.get(child) == GRAY:
                    chain = [n for n, _ in stack]
                    i = chain.index(child)
                    path = " -> ".join("#" + str(n) for n in chain[i:] + [child])
                    raise EngineError(f"引用链存在环：{path}")
                if color.get(child) == WHITE:
                    color[child] = GRAY
                    cit = by_id[child]
                    stack.append(
                        (child, iter([cit.ref_id]) if cit.start_mode == "ref" else iter(()))
                    )
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()


def resolve_starts(
    items: List[Item], by_id: Dict[int, Item]
) -> Dict[Item, int]:
    memo: Dict[int, int] = {}

    def start_of(x: Item) -> int:
        if x.id is not None and x.id in memo:
            return memo[x.id]
        if x.start_mode == "abs":
            value = x.start_ms or 0
        else:
            ref = by_id[x.ref_id]
            base = start_of(ref) if x.ref_edge == "start" else end_of(ref)
            value = base + (x.ref_offset or 0)
        if x.id is not None:
            memo[x.id] = value
        return value

    def end_of(x: Item) -> int:
        return start_of(x) + x.duration

    return {it: start_of(it) for it in items}


# --------------------------------------------------------------------------
# Full resolution: per-channel fade segments
# --------------------------------------------------------------------------

def resolve(items: Iterable[Item]) -> ResolvedView:
    items = [it for it in items if it.id is not None]
    validate(items)
    by_id = {it.id: it for it in items}
    starts = resolve_starts(items, by_id)

    ordered = sorted(items, key=lambda it: (starts[it], it.id))
    resolved: Dict[int, ResolvedItem] = {}

    grouped: Dict[Tuple[str, int], List[Item]] = {}
    for it in ordered:
        grouped.setdefault((it.fixture, it.channel), []).append(it)

    lanes: Dict[str, List[Segment]] = {}
    for (fixture, channel), lane_items in grouped.items():
        lane_items.sort(key=lambda it: (starts[it], it.id))
        segs: List[Segment] = []
        value = Fraction(0)  # current unrounded channel value
        for it in lane_items:
            s = starts[it]
            e = s + it.duration
            # value of the prior fade at time s (the fade may be cut short)
            value = _value_at(segs, s, value)
            target = Fraction(it.target)
            if it.duration == 0:
                segs.append(Segment(s, s, target, target, True, it.id))
                value = target
                interrupted_by = None
            else:
                segs.append(Segment(s, e, value, target, False, it.id))
                value = target
                nxt = next((j for j in lane_items if starts[j] > s), None)
                interrupted_by = nxt.id if nxt is not None and starts[nxt] < e else None
            resolved[it.id] = ResolvedItem(it, s, e, interrupted_by)
        lanes[f"{fixture}|{channel}"] = segs

    out = [resolved[it.id] for it in items]  # reference/input order
    return ResolvedView(items=out, lanes=lanes)


def _value_at(segs: List[Segment], t: int, fallback: Fraction) -> Fraction:
    for seg in reversed(segs):
        if seg.instant:
            if t >= seg.begin:
                return seg.end_value
            continue
        if t >= seg.end:
            return seg.end_value
        if t >= seg.begin:
            if seg.end == seg.begin:
                return seg.end_value
            frac = Fraction(t - seg.begin, seg.end - seg.begin)
            return seg.start_value + (seg.end_value - seg.start_value) * frac
    return fallback


# An evaluation instant.  Item starts/durations stay integer milliseconds,
# but the playhead may sit at any non-negative fractional millisecond; an
# int is accepted everywhere a Time is expected (Fraction arithmetic mixes).
Time = Fraction


# --------------------------------------------------------------------------
# Evaluation at an arbitrary absolute time
# --------------------------------------------------------------------------

def eval_lane(segs: List[Segment], t: Time) -> Tuple[Fraction, Optional[int]]:
    """Return (exact value, item id responsible for the value) at time t.

    Segments are start-ordered; an interrupted segment can still span t,
    so the *last* segment whose begin <= t wins.
    """
    value = Fraction(0)
    active: Optional[int] = None
    for seg in segs:
        if t < seg.begin:
            break
        if seg.instant:
            value = seg.end_value
            active = seg.item_id
            continue
        if t >= seg.end:
            value = seg.end_value
        else:
            frac = Fraction(t - seg.begin, seg.end - seg.begin)
            value = seg.start_value + (seg.end_value - seg.start_value) * frac
        active = seg.item_id
    return value, active


def evaluate(view: ResolvedView, t: Time) -> Dict[str, dict]:
    if t < 0:
        raise EngineError("时间不能为负")
    out: Dict[str, dict] = {}
    for key, segs in view.lanes.items():
        raw, active = eval_lane(segs, t)
        fixture, channel = key.split("|", 1)
        out[key] = {
            "fixture": fixture,
            "channel": int(channel),
            "raw": str(raw),
            "value": round_half_up(raw),
            "active_item_id": active,
        }
    return out


def round_half_up(x: Fraction) -> int:
    """Round to nearest integer; exact .5 goes up (away from zero)."""
    q, r = divmod(x.numerator, x.denominator)
    if r * 2 >= x.denominator:
        q += 1
    return q


# --------------------------------------------------------------------------
# Reference graph helpers (for the UI chain highlighting)
# --------------------------------------------------------------------------

def referenced_by_map(items: Iterable[Item]) -> Dict[int, set]:
    """item id -> set of ids that reference it directly."""
    refs: Dict[int, set] = {}
    for it in items:
        if it.start_mode == "ref" and it.ref_id is not None:
            refs.setdefault(it.ref_id, set()).add(it.id)
    return refs


def referrer_chain(items: Iterable[Item], target_id: int) -> List[int]:
    """Transitive reverse closure: items whose start (in)directly refs target."""
    result: List[int] = []
    seen = {target_id}
    frontier = [target_id]
    refs = referenced_by_map(items)
    while frontier:
        cur = frontier.pop()
        for nxt in sorted(refs.get(cur, ())):
            if nxt not in seen:
                seen.add(nxt)
                result.append(nxt)
                frontier.append(nxt)
    return result
