from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from app.config import settings
from app.db import init_db
from app.routes import admin, join, leaderboard, player
from app.sync import run_sync_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def check_config() -> None:
    """Log configuration problems at startup so they show in deploy logs."""
    if not settings.admin_token:
        logger.warning("ADMIN_TOKEN is not set — admin endpoints will return 503")
    if settings.cred_secret:
        from cryptography.fernet import Fernet

        try:
            Fernet(settings.cred_secret.encode())
        except Exception:
            logger.error(
                "CRED_SECRET is set but is NOT a valid Fernet key — adding Kalshi "
                "accounts will fail. Generate one with: python -c "
                '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
    else:
        logger.warning("CRED_SECRET is not set — Kalshi keys will be stored unencrypted")


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_config()
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
app.include_router(player.router)
app.include_router(join.router)
app.include_router(admin.router)
