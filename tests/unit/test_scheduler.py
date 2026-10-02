"""The scheduler and the Cloud Scheduler webhook queue rows; neither runs an agent."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

from dot.assembly import build_graph_runtime
from dot.config import Settings
from dot.persistence.db import Dot, MemoryRepositories, NotFound, User
from dot.proactive.cron import cron_trigger
from dot.proactive.scheduler import fire, install, trigger
from dot.runtime.turns import InMemoryEventChannel
from dot.surfaces.api import create_app
from dot.surfaces.identity import SchedulerVerifier

WHEN = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
AUDIENCE = "https://api.example.com"
INVOKER = "scheduler@project.iam.gserviceaccount.com"


def _repos() -> MemoryRepositories:
    repos = MemoryRepositories()
    repos.create_user(User("u1", "Ada"))
    repos.create_dot(Dot("d1", "u1", "research-analyst", "0", "d1", "active", WHEN))
    repos.create_dot(Dot("d2", "u1", "research-analyst", "0", "d2", "paused", WHEN))
    return repos


def _fires(expression: str, start: datetime, count: int) -> list[str]:
    trigger = cron_trigger(expression, ZoneInfo("Europe/London"))
    fired: list[str] = []
    at: datetime | None = start
    for _ in range(count):
        at = trigger.get_next_fire_time(None, at)
        assert at is not None
        fired.append(at.strftime("%a %H:%M %Z"))
        at = at.replace(second=1)
    return fired


def test_crontab_weekdays_mean_monday_to_friday() -> None:
    saturday = datetime(2026, 10, 3, 0, 0, tzinfo=ZoneInfo("Europe/London"))
    assert _fires("45 8 * * 1-5", saturday, 6) == [
        "Mon 08:45 BST",
        "Tue 08:45 BST",
        "Wed 08:45 BST",
        "Thu 08:45 BST",
        "Fri 08:45 BST",
        "Mon 08:45 BST",
    ]
    assert _fires("0 9 * * 0", saturday, 1) == ["Sun 09:00 BST"]
    assert _fires("0 9 * * 5-7", saturday, 3) == ["Sat 09:00 BST", "Sun 09:00 BST", "Fri 09:00 BST"]


@pytest.mark.parametrize("expression", ["* * * *", "0 9 * * */2", "0 9 * * 8", "61 * * * *"])
def test_bad_crons_are_refused(expression: str) -> None:
    with pytest.raises(ValueError):
        cron_trigger(expression, UTC)


def test_trigger_queues_the_packs_profile_and_prompt_and_absorbs_repeats() -> None:
    repos = _repos()
    first = trigger(repos, repos.get_dot("d1"), "sweep", WHEN.replace(second=42))
    assert first is not None
    assert (first.source, first.profile) == ("schedule", "sweep")
    assert first.payload == {"text": "Run your sweep.", "schedule": "sweep", "slot": "2026-10-05T09:00:00+00:00"}
    assert trigger(repos, repos.get_dot("d1"), "sweep", WHEN.replace(minute=30)) is None
    assert trigger(repos, repos.get_dot("d2"), "sweep", WHEN) is None
    with pytest.raises(NotFound):
        trigger(repos, repos.get_dot("d1"), "nightly", WHEN)


def test_fire_queues_every_active_dot_of_the_pack() -> None:
    repos = _repos()
    repos.create_dot(Dot("d3", "u1", "research-analyst", "0", "d3", "active", WHEN))
    queued = fire(repos, "research-analyst", "digest", WHEN)
    assert sorted(message.dot_id for message in queued) == ["d1", "d3"]
    assert {message.profile for message in queued} == {"digest"}


def test_install_adds_one_cron_job_per_pack_schedule(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, schedule_timezone="Europe/London")  # type: ignore[call-arg]
    scheduler = BackgroundScheduler(timezone=ZoneInfo("Europe/London"))
    assert install(scheduler, _repos(), settings) == [
        "onboarding-ops:intake-sweep",
        "onboarding-ops:status-sweep",
        "onboarding-ops:intake",
        "onboarding-ops:daily",
        "onboarding-ops:reflection",
        "research-analyst:sweep",
        "research-analyst:digest",
        "research-analyst:reflection",
    ]
    job = scheduler.get_job("research-analyst:digest")
    assert job is not None and job.coalesce and job.max_instances == 1
    saturday = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("Europe/London"))
    fired = job.trigger.get_next_fire_time(None, saturday)
    assert fired is not None and fired.strftime("%a %H:%M") == "Mon 08:45"


def test_settings_refuse_an_unknown_zone_or_half_a_scheduler_identity() -> None:
    with pytest.raises(ValueError, match="DOT_SCHEDULE_TIMEZONE"):
        Settings(_env_file=None, schedule_timezone="Mars/Olympus")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="DOT_SCHEDULER_AUDIENCE"):
        Settings(_env_file=None, scheduler_audience=AUDIENCE)  # type: ignore[call-arg]


class _Google:
    """Signs OIDC tokens the way Google does for Cloud Scheduler."""

    def __init__(self) -> None:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        self.signer = crypt.RSASigner.from_string(pem, key_id="g1")
        public = private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.certs = {"g1": public.decode()}

    def fetch(self) -> Mapping[str, str]:
        return self.certs

    def token(self, **claims: Any) -> str:
        now = int(time.time())
        payload = {
            "iss": "https://accounts.google.com",
            "aud": AUDIENCE,
            "email": INVOKER,
            "email_verified": True,
            "sub": "1234",
            "iat": now,
            "exp": now + 600,
        }
        payload.update(claims)
        return jwt.encode(self.signer, payload).decode()  # type: ignore[no-untyped-call]


def _client(tmp_path: Path, repos: MemoryRepositories, google: _Google | None) -> TestClient:
    settings = Settings(_env_file=None, object_root=str(tmp_path))  # type: ignore[call-arg]
    app = create_app(
        settings,
        repos=repos,
        runtime=build_graph_runtime(settings),
        events=InMemoryEventChannel(),
        scheduler_verifier=SchedulerVerifier(AUDIENCE, INVOKER, google.fetch) if google is not None else None,
    )
    return TestClient(app)


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def test_the_webhook_fails_closed_when_unconfigured(tmp_path: Path) -> None:
    with _client(tmp_path, _repos(), None) as client:
        assert client.post("/schedules/d1/sweep").status_code == 503


def test_the_webhook_queues_a_run_for_a_verified_scheduler_token(tmp_path: Path) -> None:
    repos = _repos()
    google = _Google()
    slot = {"x-cloudscheduler-scheduletime": "2026-10-05T09:30:00.123Z"}
    with _client(tmp_path, repos, google) as client:
        response = client.post("/schedules/d1/sweep", headers=_bearer(google.token()) | slot, json={"profile": "chat"})
        assert response.status_code == 202
        body = response.json()
        assert body["queued"] is True
        stored = repos.get_inbox(body["inbox_id"])
        # The body is ignored: profile and prompt are the pack's.
        assert stored.profile == "sweep" and stored.payload["slot"] == "2026-10-05T09:30:00+00:00"
        # A retry of the same attempt is absorbed.
        retry = client.post("/schedules/d1/sweep", headers=_bearer(google.token()) | slot)
        assert retry.status_code == 202 and retry.json()["queued"] is False
        assert client.post("/schedules/d1/nightly", headers=_bearer(google.token())).status_code == 404
        assert client.post("/schedules/nope/sweep", headers=_bearer(google.token())).status_code == 404


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "https://other.example.com"},
        {"email": "someone@example.com"},
        {"email_verified": False},
        {"iss": "https://evil.example.com"},
        {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200},
    ],
)
def test_the_webhook_refuses_other_tokens(tmp_path: Path, claims: dict[str, Any]) -> None:
    repos = _repos()
    google = _Google()
    with _client(tmp_path, repos, google) as client:
        assert client.post("/schedules/d1/sweep", headers=_bearer(google.token(**claims))).status_code == 401
        # Unauthenticated callers learn nothing, not even whether the dot exists.
        assert client.post("/schedules/nope/sweep", headers=_bearer(google.token(**claims))).status_code == 401
        assert client.post("/schedules/d1/sweep", headers=_bearer(_Google().token())).status_code == 401
        assert client.post("/schedules/d1/sweep").status_code == 401
    assert repos.inbox == {}
