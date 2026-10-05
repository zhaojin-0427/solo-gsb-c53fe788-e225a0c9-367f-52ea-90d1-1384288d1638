"""FastAPI application: project/item CRUD plus exact-time evaluation."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from fractions import Fraction
from typing import Optional, Union

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .engine import (
    EngineError,
    Item,
    evaluate,
    referrer_chain,
    resolve,
)
from .schemas import ItemIn, ProjectIn
from .seed import seed_if_empty

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    seed_if_empty()
    yield


app = FastAPI(title="灯光渐变时间轴", version="1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

def item_to_dict(it: Item, resolved, all_items) -> dict:
    refs_me = referrer_chain(all_items, it.id)
    return {
        "id": it.id,
        "fixture": it.fixture,
        "channel": it.channel,
        "target": it.target,
        "duration": it.duration,
        "start_mode": it.start_mode,
        "start_ms": it.start_ms,
        "ref_id": it.ref_id,
        "ref_edge": it.ref_edge,
        "ref_offset": it.ref_offset,
        "actual_start": resolved.start,
        "actual_end": resolved.end,
        "interrupted_by": resolved.interrupted_by,
        "referrers": refs_me,
    }


def project_payload(conn, project_id: int) -> dict:
    items = db.load_items(conn, project_id)
    view = resolve(items)
    resolved_by_id = {r.item.id: r for r in view.items}
    return {
        "id": project_id,
        "name": db.project_name(conn, project_id),
        "items": [
            item_to_dict(it, resolved_by_id[it.id], items) for it in items
        ],
        "duration": max((r.end for r in view.items), default=0),
    }


def to_item(data: ItemIn, item_id: Optional[int] = None) -> Item:
    return Item(
        id=item_id,
        fixture=data.fixture.strip(),
        channel=data.channel,
        target=data.target,
        duration=data.duration,
        start_mode=data.start_mode,
        start_ms=data.start_ms if data.start_mode == "abs" else None,
        ref_id=data.ref_id if data.start_mode == "ref" else None,
        ref_edge=data.ref_edge if data.start_mode == "ref" else None,
        ref_offset=data.ref_offset if data.start_mode == "ref" else None,
    )


def get_project_or_404(conn, project_id: int):
    if not db.project_exists(conn, project_id):
        raise HTTPException(status_code=404, detail="工程不存在")


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------

@app.get("/api/projects")
def api_list_projects():
    with db.transaction() as conn:
        out = []
        for row in db.list_projects(conn):
            pid = row["id"]
            items = db.load_items(conn, pid)
            end = max((r.end for r in resolve(items).items), default=0)
            out.append({"id": pid, "name": row["name"], "item_count": len(items),
                        "duration": end})
        return out


@app.post("/api/projects", status_code=201)
def api_create_project(data: ProjectIn):
    with db.transaction() as conn:
        pid = db.create_project(conn, data.name.strip())
        return project_payload(conn, pid)


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: int):
    with db.transaction() as conn:
        get_project_or_404(conn, project_id)
        return project_payload(conn, project_id)


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/items", status_code=201)
def api_create_item(project_id: int, data: ItemIn):
    with db.transaction() as conn:
        get_project_or_404(conn, project_id)
        order = db.next_sort_order(conn, project_id)
        new_id = db.insert_item(conn, project_id, to_item(data), order)
        return _validate_and_payload(conn, project_id, new_id)


@app.put("/api/projects/{project_id}/items/{item_id}")
def api_update_item(project_id: int, item_id: int, data: ItemIn):
    with db.transaction() as conn:
        get_project_or_404(conn, project_id)
        if data.start_mode == "ref" and data.ref_id == item_id:
            raise HTTPException(status_code=400, detail="提示项不能引用自身")
        db.update_item(conn, project_id, to_item(data, item_id))
        return _validate_and_payload(conn, project_id, item_id)


@app.delete("/api/projects/{project_id}/items/{item_id}")
def api_delete_item(project_id: int, item_id: int):
    with db.transaction() as conn:
        get_project_or_404(conn, project_id)
        referenced = db.is_referenced(conn, project_id, item_id)
        if referenced:
            raise HTTPException(
                status_code=409,
                detail=f"该提示项被 #{'、#'.join(map(str, referenced))} 引用，不能删除",
            )
        db.delete_item_row(conn, project_id, item_id)
        return _validate_and_payload(conn, project_id, None)


def _validate_and_payload(conn, project_id: int, focus_id: Optional[int]):
    """Re-validate the entire resulting project; rollback on any error.

    The row writes above are uncommitted.  ``resolve`` raises
    EngineError on a missing reference / cycle / same-instant collision,
    which aborts the transaction so the previously saved project is kept.
    """
    try:
        payload = project_payload(conn, project_id)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.detail)
    payload["focus_item_id"] = focus_id
    return payload


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

class EvalResponse(BaseModel):
    # int when t is integral, float otherwise; never lossy because the
    # exact value was already consumed by the Fraction engine before
    # serialization.
    time_ms: Union[int, float]
    channels: list


def parse_time(t: str) -> Fraction:
    """Parse a query time into an exact Fraction.

    Accepts non-negative decimal text ("1.5", "1_000", "1e-3") and
    rational text ("5/3") exactly as fractions.Fraction does.  Raises
    HTTP 422 on malformed, infinite/NaN or negative input.
    """
    raw = t.strip()
    if not raw:
        raise HTTPException(status_code=422, detail="时间参数 t 不能为空")
    try:
        value = Fraction(raw)
    except (ValueError, ZeroDivisionError):
        raise HTTPException(
            status_code=422, detail=f"时间参数 t={t!r} 不是合法的非负小数时刻"
        )
    if value < 0:
        raise HTTPException(status_code=422, detail="时间不能为负")
    return value


@app.get("/api/projects/{project_id}/eval", response_model=EvalResponse)
def api_eval(project_id: int, t: str = Query(..., description="非负小数毫秒时刻，如 1.5 或 5/3")):
    time = parse_time(t)
    with db.transaction() as conn:
        get_project_or_404(conn, project_id)
        items = db.load_items(conn, project_id)
        try:
            view = resolve(items)
            channels = evaluate(view, time)
        except EngineError as exc:
            raise HTTPException(status_code=400, detail=exc.detail)
        ordered = sorted(channels.values(), key=lambda c: (c["fixture"], c["channel"]))
        time_ms = time.numerator // time.denominator if time.denominator == 1 \
            else time.numerator / time.denominator
        return {"time_ms": time_ms, "channels": ordered}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
