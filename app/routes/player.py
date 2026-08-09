from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db import db_session
from app.links import sport_slug
from app.models import Participant
from app.routes.leaderboard import _ago, sparkline_svg
from app.scoring import compute_standings
from app.stats import compute_player_stats

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/player/{participant_id}", response_class=HTMLResponse)
def player_page(request: Request, participant_id: int):
    with db_session() as db:
        participant = db.get(Participant, participant_id)
        if participant is None:
            raise HTTPException(404, "no such player")
        standings = compute_standings(db)
        standing = next(
            (s for s in standings if s.participant_id == participant_id), None
        )
        rank = next(
            (i + 1 for i, s in enumerate(standings) if s.participant_id == participant_id),
            None,
        )
        stats = compute_player_stats(db, participant)
        chart = sparkline_svg(
            standing.history if standing else [], width=640, height=140, css_class="chart"
        )
        max_cat_wagered = max((c.wagered for c in stats.categories), default=0.0)
        return templates.TemplateResponse(
            request,
            "player.html",
            {
                "p": participant,
                "s": standing,
                "rank": rank,
                "stats": stats,
                "chart": chart,
                "max_cat_wagered": max_cat_wagered,
                "ago": _ago,
                "sport_slug": sport_slug,
            },
        )
