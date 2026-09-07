"""Auto-apply retry memory: permanent failures never retry; others wait a cooldown.

Before 2026-09-01 the runner re-attempted the same login-walled job every
cycle, hit the consecutive-failure halt, and never reached other candidates.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agents.auto_applier.runner import _skip_after_failure

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def _app(status, hours_ago=1):
    return SimpleNamespace(auto_apply_status=status,
                           auto_apply_attempted_at=NOW - timedelta(hours=hours_ago))


def test_never_attempted_is_eligible():
    app = SimpleNamespace(auto_apply_status=None, auto_apply_attempted_at=None)
    assert not _skip_after_failure(app, NOW, 24)


def test_login_wall_and_captcha_are_permanent():
    assert _skip_after_failure(_app("failed_login_required", hours_ago=24 * 30), NOW, 24)
    assert _skip_after_failure(_app("failed_workday_login_required", hours_ago=24 * 30), NOW, 24)
    assert _skip_after_failure(_app("failed_captcha", hours_ago=24 * 30), NOW, 24)


def test_retryable_failure_waits_for_cooldown():
    assert _skip_after_failure(_app("failed_submit_button", hours_ago=2), NOW, 24)
    assert not _skip_after_failure(_app("failed_submit_button", hours_ago=25), NOW, 24)


def test_naive_timestamp_is_treated_as_utc():
    app = SimpleNamespace(auto_apply_status="failed_unknown",
                          auto_apply_attempted_at=(NOW - timedelta(hours=2)).replace(tzinfo=None))
    assert _skip_after_failure(app, NOW, 24)


def test_dry_run_and_submitted_never_block():
    assert not _skip_after_failure(_app("dry_run"), NOW, 24)
    assert not _skip_after_failure(_app("submitted"), NOW, 24)


def test_mapper_engine_outcomes_are_permanent():
    # 2026-09-07: blank required fields need a human; an unconfirmed Submit
    # click must never be retried or we could apply twice.
    assert _skip_after_failure(_app("failed_required_fields_unfilled", hours_ago=24 * 30), NOW, 24)
    assert _skip_after_failure(_app("submitted_unverified", hours_ago=24 * 30), NOW, 24)
