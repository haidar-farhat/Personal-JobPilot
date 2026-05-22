"""Tests for the /api/stream Server-Sent Events feed."""

import json
import time

import pytest
import requests


pytestmark = pytest.mark.live


REQUIRED_STAT_KEYS = {
    "total_jobs",
    "jobs_today",
    "jobs_this_week",
    "pipeline",
    "avg_score",
    "high_matches",
    "needs_followup",
    "response_rate",
    "pending_review",
    "last_update",
}


def _read_sse_events(url: str, count: int, max_seconds: float = 20.0):
    """Read up to `count` SSE events from `url`, returning the parsed JSON payloads."""
    events: list[dict] = []
    start = time.time()

    with requests.get(url, stream=True, timeout=max_seconds) as r:
        r.raise_for_status()
        buf = ""
        for chunk in r.iter_content(chunk_size=None, decode_unicode=True):
            if not chunk:
                continue
            buf += chunk
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                for line in raw.splitlines():
                    if line.startswith("data: "):
                        try:
                            events.append(json.loads(line[6:]))
                        except json.JSONDecodeError:
                            pass
                if len(events) >= count:
                    return events
            if time.time() - start > max_seconds:
                break
    return events


def test_sse_emits_stats_within_six_seconds(base_url):
    events = _read_sse_events(f"{base_url}/api/stream", count=1, max_seconds=8)
    assert events, "no SSE events received within 8s"
    assert events[0].get("type") == "stats"


def test_sse_stats_payload_shape(base_url):
    events = _read_sse_events(f"{base_url}/api/stream", count=1, max_seconds=8)
    assert events
    payload = events[0]["payload"]
    missing = REQUIRED_STAT_KEYS - set(payload.keys())
    assert not missing, f"stats payload missing keys: {missing}"
    assert isinstance(payload["total_jobs"], int)
    assert isinstance(payload["pipeline"], dict)
    assert payload["avg_score"] >= 0
    assert payload["response_rate"] >= 0


def test_sse_emits_repeatedly(base_url):
    """Verify the stream keeps ticking — should see at least 2 stats events in 12s."""
    events = _read_sse_events(f"{base_url}/api/stream", count=4, max_seconds=15)
    stats_events = [e for e in events if e.get("type") == "stats"]
    assert len(stats_events) >= 2, f"expected >=2 stats ticks in 15s, got {len(stats_events)}"
