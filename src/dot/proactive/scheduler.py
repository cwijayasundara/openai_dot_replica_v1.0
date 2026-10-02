"""Turn pack schedules into inbox rows. See design section 7.

Locally one scheduler process runs APScheduler with one cron job per pack
schedule; each firing queues a row for every active dot of that pack, so dots
created later are covered. On GCP, Cloud Scheduler calls
``POST /schedules/{dot}/{name}`` instead. Both call ``trigger``; neither runs an
agent. A firing is dropped while that schedule's last row is still pending, and
a slot that already ran is not queued twice.
"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler

from dot.config import Settings, get_settings
from dot.packs.loader import REPO_ROOT, load_pack
from dot.persistence.db import Dot, InboxMessage, NotFound, Repositories, open_repositories
from dot.proactive.cron import cron_trigger

log = logging.getLogger(__name__)

PACKS = REPO_ROOT / "packs"
# A firing missed by up to this much (a restart, a long pause) still runs once.
MISFIRE_GRACE_S = 300


def trigger(repos: Repositories, dot: Dot, name: str, at: datetime) -> InboxMessage | None:
    """Queue schedule ``name`` for ``dot`` at slot ``at``. None when nothing was queued.

    The profile and prompt are the pack's. Raises NotFound for a schedule the pack lacks.
    """
    if dot.status != "active":
        return None
    pack = load_pack(PACKS / dot.pack_name).pack
    try:
        schedule = pack.schedule(name)
    except KeyError:
        raise NotFound("schedules", f"{dot.pack_name}/{name}") from None
    slot = at.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
    payload = {"text": schedule.prompt, "schedule": schedule.name, "slot": slot}
    return repos.insert_schedule_run(
        InboxMessage(0, dot.dot_id, "schedule", payload, schedule.inbox_profile, datetime.now(UTC))
    )


def fire(repos: Repositories, pack_name: str, name: str, at: datetime | None = None) -> list[InboxMessage]:
    """One firing of a pack schedule, for every active dot of the pack."""
    at = at or datetime.now(UTC)
    queued: list[InboxMessage] = []
    for dot in repos.list_active_dots(pack_name):
        try:
            message = trigger(repos, dot, name, at)
        except Exception:
            # One dot's failure must not starve the others of this firing.
            log.exception("schedule %s/%s failed for %s", pack_name, name, dot.dot_id)
            continue
        if message is not None:
            queued.append(message)
    return queued


def install(scheduler: BaseScheduler, repos: Repositories, settings: Settings, packs: Path = PACKS) -> list[str]:
    """Add one cron job per pack schedule. Returns the job ids."""
    zone = ZoneInfo(settings.schedule_timezone)
    ids: list[str] = []
    for pack_dir in sorted(packs.iterdir()):
        if not (pack_dir / "pack.yaml").is_file():
            continue
        pack = load_pack(pack_dir).pack
        for schedule in pack.schedules:
            job_id = f"{pack.name}:{schedule.name}"
            scheduler.add_job(
                fire,
                cron_trigger(schedule.cron, zone),
                args=(repos, pack.name, schedule.name),
                id=job_id,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=MISFIRE_GRACE_S,
                replace_existing=True,
            )
            ids.append(job_id)
    return ids


def serve(settings: Settings | None = None) -> None:
    """Run the local scheduler until SIGINT or SIGTERM. Run one per deployment."""
    settings = settings or get_settings()
    if not settings.database_url:
        raise SystemExit("DOT_DATABASE_URL is required for the scheduler")
    repos = open_repositories(settings.database_url)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    scheduler = BackgroundScheduler(timezone=ZoneInfo(settings.schedule_timezone))
    try:
        for job_id in install(scheduler, repos, settings):
            log.info("scheduled %s", job_id)
        scheduler.start()
        stop.wait()
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=True)
        repos.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    serve()


if __name__ == "__main__":
    main()
