from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db import db_session
from app.models import Fill, MarketMeta
from app.scoring import Standing, compute_standings, recent_fills
from app.stats import fill_metas

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def sparkline_svg(
    history: list[tuple[datetime, float]],
    width: int = 120,
    height: int = 32,
    css_class: str = "spark",
) -> str:
    """Inline SVG line of P&L history. Stroke color comes from CSS; a dashed
    zero line is drawn when the values cross it."""
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
    zero_line = ""
    if lo < 0 < hi:
        zy = pad + hi * (height - 2 * pad) / span
        zero_line = (
            f'<line x1="{pad}" y1="{zy:.1f}" x2="{width - pad}" y2="{zy:.1f}" '
            f'class="zero" stroke-dasharray="3 3" stroke-width="1"/>'
        )
    return (
        f'<svg class="{css_class}" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="P&L trend">{zero_line}'
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


def outcome_label(outcome: str, meta: MarketMeta | None) -> str:
    sub = meta.yes_sub_title if meta else ""
    if sub and sub != outcome:
        if outcome == "Yes":
            return sub
        if outcome == "No":
            return f"No — {sub}"
    return outcome


def feed_item(f: Fill, meta: MarketMeta | None) -> dict:
    label = outcome_label(f.outcome, meta)
    title = (meta.title if meta and meta.title else f.market_title) or f.market_key
    base = {
        "who": f.account.participant.name,
        "participant_id": f.account.participant_id,
        "platform": f.platform,
        "category": f.category or (meta.category if meta else ""),
        "title": title,
        "ago": _ago(f.ts if f.ts.tzinfo else f.ts.replace(tzinfo=timezone.utc)),
    }
    if f.kind == "settlement":
        if f.notional > 0:
            base["headline"] = f"won ${f.notional:,.2f}"
            base["tone"] = "up"
        else:
            base["headline"] = "lost a bet"
            base["tone"] = "down"
        base["detail"] = f"settled {label}" if label else "settled"
    elif f.side == "buy":
        base["headline"] = f"put ${f.notional:,.2f} on {label}"
        base["tone"] = "buy"
        base["detail"] = f"@ {f.price * 100:.0f}¢ · {f.size:.0f} contracts"
    else:
        base["headline"] = f"sold {label} for ${f.notional:,.2f}"
        base["tone"] = "sell"
        base["detail"] = f"@ {f.price * 100:.0f}¢ · {f.size:.0f} contracts"
    return base


def _standing_json(s: Standing) -> dict:
    return {
        "participant": s.name,
        "participant_id": s.participant_id,
        "game_value": round(s.game_value, 2),
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
def leaderboard_page(request: Request, welcome: str | None = None):
    with db_session() as db:
        standings = compute_standings(db)
        fills = recent_fills(db, limit=30)
        metas = fill_metas(db, fills)
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
        feed = [feed_item(f, metas.get((f.platform, f.market_key))) for f in fills]
        return templates.TemplateResponse(
            request,
            "leaderboard.html",
            {
                "rows": rows,
                "feed": feed,
                "welcome": welcome,
                "now": datetime.now(timezone.utc),
            },
        )
