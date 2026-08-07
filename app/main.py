from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from app.config import settings
from app.db import init_db
from app.routes import admin, leaderboard
from app.sync import run_sync_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_sync_all,
        "interval",
        minutes=settings.sync_interval_minutes,
        id="sync",
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Prediction Draft Tracker", lifespan=lifespan)
app.include_router(leaderboard.router)
app.include_router(admin.router)
