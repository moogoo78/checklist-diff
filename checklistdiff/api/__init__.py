"""FastAPI application.

Read-only by design: ingest and diff are CLI operations, so no route here can
mutate the database. That keeps the append-only guarantee on `usage` something
the schema enforces rather than something the UI has to be careful about.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text

from checklistdiff import __version__
from checklistdiff.db import get_engine, session_scope
from checklistdiff.models import Checklist
from checklistdiff.trace.resolve import resolve
from checklistdiff.trace.timeline import build_trace, trace_to_dict

TEMPLATES = Path(__file__).parent / "templates"

app = FastAPI(title="ChecklistDiff", version=__version__)
templates = Jinja2Templates(directory=str(TEMPLATES))


def describe_change(detail: dict | None, kind: str) -> str:
    """Render a change's detail blob as a short human phrase."""
    d = detail or {}
    match kind:
        case "lumped":
            return f"{', '.join(d.get('sunk', []))} → {d.get('into')}"
        case "split":
            return f"{d.get('from')} → {', '.join(d.get('into', []))}"
        case "accepted_changed":
            return f"{d.get('from_accepted') or '—'} → {d.get('to_accepted') or '—'}"
        case "status_changed":
            return f"{d.get('from_status')} → {d.get('to_status')}"
        case "reclassified":
            return f"parent {d.get('from_parent') or '—'} → {d.get('to_parent') or '—'}"
        case "rank_changed":
            return f"{d.get('from_rank')} → {d.get('to_rank')}"
        case "renamed" | "probable_rename":
            return f"{d.get('from_name')} → {d.get('to_name')}"
        case "author_changed":
            return f"{d.get('from_author') or '—'} → {d.get('to_author') or '—'}"
        case "id_replaced":
            return f"{d.get('from_id')} → {d.get('to_id')}"
        case _:
            return ""


templates.env.filters["describe_change"] = describe_change


@app.get("/healthz")
def healthz() -> dict[str, object]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001 - liveness probe must not raise
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "version": __version__, "db": db_ok}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str | None = Query(None)):
    with session_scope() as session:
        if not q:
            checklists = session.scalars(
                select(Checklist).order_by(Checklist.code)
            ).all()
            return templates.TemplateResponse(
                request=request,
                name="index.html",
                context={
                    "query": None,
                    "traces": None,
                    "checklists": [
                        {
                            "code": c.code,
                            "title": c.title,
                            "id_stability": c.id_stability,
                            "releases": [
                                {"version": r.version, "usage_count": r.usage_count}
                                for r in c.releases
                            ],
                        }
                        for c in checklists
                    ],
                },
            )

        resolution = resolve(session, q)
        traces = [trace_to_dict(build_trace(session, n)) for n in resolution.names]
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "query": q,
                "tier": resolution.tier.value,
                "traces": traces,
                "scores": resolution.scores or {},
                "checklists": [],
            },
        )


@app.get("/api/trace")
def api_trace(name: str = Query(..., min_length=1)) -> dict:
    with session_scope() as session:
        resolution = resolve(session, name)
        return {
            "query": resolution.query,
            "match": resolution.tier.value,
            "count": len(resolution.names),
            "scores": resolution.scores or {},
            "traces": [
                trace_to_dict(build_trace(session, n)) for n in resolution.names
            ],
        }


@app.get("/api/checklists")
def api_checklists() -> dict:
    with session_scope() as session:
        checklists = session.scalars(select(Checklist).order_by(Checklist.code)).all()
        return {
            "checklists": [
                {
                    "code": c.code,
                    "title": c.title,
                    "publisher": c.publisher,
                    "id_stability": c.id_stability,
                    "releases": [
                        {
                            "version": r.version,
                            "released_on": r.released_on.isoformat()
                            if r.released_on
                            else None,
                            "usage_count": r.usage_count,
                            "status": r.status,
                        }
                        for r in c.releases
                    ],
                }
                for c in checklists
            ]
        }
