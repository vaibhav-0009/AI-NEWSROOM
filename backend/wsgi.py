"""WSGI entry point for persistent hosting (use one worker to avoid duplicate schedulers)."""
from app import app, init, scheduled, os
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timezone

init()
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(scheduled, "date", run_date=datetime.now(timezone.utc), id="startup_pipeline")
scheduler.add_job(
    scheduled,
    "interval",
    minutes=max(5, int(os.getenv("COLLECTION_INTERVAL_MINUTES", "5"))),
    max_instances=1,
    coalesce=True,
)
scheduler.start()
