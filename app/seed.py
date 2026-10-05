"""Seed a small demo project on first launch (shows references + interruption)."""

from __future__ import annotations

from . import db
from .engine import Item


def seed_if_empty() -> int | None:
    with db.transaction() as conn:
        if db.list_projects(conn):
            return None
        pid = db.create_project(conn, "演示工程")
        order = 0

        def add(**kw):
            nonlocal order
            order += 1
            return db.insert_item(conn, pid, Item(id=None, **kw), order)

        i1 = add(fixture="面光1", channel=1, target=180, duration=2000,
                 start_mode="abs", start_ms=0)
        add(fixture="面光1", channel=2, target=120, duration=1000,
            start_mode="abs", start_ms=500)
        i3 = add(fixture="顶光1", channel=1, target=255, duration=0,
                 start_mode="abs", start_ms=0)
        # starts when the first fade ends
        add(fixture="面光1", channel=1, target=60, duration=1500,
            start_mode="ref", ref_id=i1, ref_edge="end", ref_offset=0)
        # starts 200ms after the instant snap
        add(fixture="顶光1", channel=1, target=100, duration=800,
            start_mode="ref", ref_id=i3, ref_edge="start", ref_offset=200)
        i6 = add(fixture="面光1", channel=2, target=255, duration=4000,
                 start_mode="ref", ref_id=i1, ref_edge="start", ref_offset=1000)
        # this interrupts the long fade of i6
        add(fixture="面光1", channel=2, target=0, duration=500,
            start_mode="ref", ref_id=i6, ref_edge="start", ref_offset=1500)
        return pid
