from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db import db_session
from app.scoring import Standing, compute_standings, recent_fills

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def sparkline_svg(history: list[tuple[datetime, float]], width: int = 120, height: int = 32) -> str:
    """Inline SVG sparkline of P&L history. Stroke color is set via CSS class."""
    if len(history) < 2:
        return ""
    values = [v for _, v in history]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pad = 3
    points = []
    for i, v in enumerate(values):
        x = pad + i * (width - 2 * pad) / (len(values) - 1)
        y = pad + (hi - v) * (height - 2 * pad) / span
        points.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="P&L trend">'
        f'<polyline fill="none" stroke-width="2" stroke-linejoin="round" '
        f'stroke-linecap="round" points="{" ".join(points)}"/></svg>'
    )


def _ago(ts: datetime | None) -> str:
    if ts is None:
        return "never"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f}d ago"


def _standing_json(s: Standing) -> dict:
    return {
        "participant": s.name,
        "current_total": round(s.current_total, 2),
        "baseline_total": round(s.baseline_total, 2),
        "pnl": round(s.pnl, 2),
        "pnl_pct": round(s.pnl_pct, 2),
        "flags": s.flags,
        "accounts": [
            {
                "platform": a.platform,
                "current_total": a.current_total,
                "baseline_total": a.baseline_total,
                "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
                "error": a.last_sync_error,
            }
            for a in s.accounts
        ],
        "history": [[ts.isoformat(), round(v, 2)] for ts, v in s.history],
    }


@router.get("/api/leaderboard")
def api_leaderboard():
    with db_session() as db:
        standings = compute_standings(db)
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "standings": [_standing_json(s) for s in standings],
        }


@router.get("/", response_class=HTMLResponse)
def leaderboard_page(request: Request):
    with db_session() as db:
        standings = compute_standings(db)
        fills = recent_fills(db, limit=30)
        rows = [
            {
                "rank": i + 1,
                "s": s,
                "spark": sparkline_svg(s.history),
                "synced": _ago(
                    min(
                        (a.last_sync_at for a in s.accounts if a.last_sync_at),
                        default=None,
                    )
                ),
            }
            for i, s in enumerate(standings)
        ]
        feed = [
            {
                "who": f.account.participant.name,
                "platform": f.platform,
                "side": f.side,
                "size": f.size,
                "outcome": f.outcome,
                "price": f.price,
                "title": f.market_title,
                "ago": _ago(f.ts),
            }
            for f in fills
        ]
        return templates.TemplateResponse(
            request,
            "leaderboard.html",
            {"rows": rows, "feed": feed, "now": datetime.now(timezone.utc)},
        )
