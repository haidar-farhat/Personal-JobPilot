"""Learn which addresses bounce, so a bad guess is never repeated.

A constructed address (careers@domain) can pass an MX check and still not
exist. Re-sending to a dead mailbox on every cycle is exactly the behaviour
that gets a sending account flagged, so this reads the delivery-failure notices
Gmail already delivers and records the addresses that failed.

Read-only IMAP, reusing agents/email_reader.recent_messages(). Nothing is
marked read, moved, sent or deleted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
_DEAD_PATH = ROOT / "output" / "dead_addresses.json"

_BOUNCE_SENDERS = ("mailer-daemon", "postmaster")
_BOUNCE_SUBJECTS = ("delivery status notification", "undeliverable",
                    "delivery has failed", "returned mail", "mail delivery failed",
                    "address not found", "delivery incomplete")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def load_dead() -> set[str]:
    """Addresses a previous send bounced from."""
    try:
        data = json.loads(_DEAD_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if isinstance(data, dict):
        return {str(a).lower() for a in data.get("addresses", [])}
    return {str(a).lower() for a in data} if isinstance(data, list) else set()


def _save_dead(addrs: set[str]) -> None:
    try:
        _DEAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"addresses": sorted(addrs)}
        fd, tmp = tempfile.mkstemp(dir=str(_DEAD_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp, _DEAD_PATH)
    except OSError as e:
        logger.warning(f"[bounce_watch] could not save dead list: {e}")


def mark_dead(address: str) -> None:
    a = (address or "").strip().lower()
    if not a:
        return
    dead = load_dead()
    if a not in dead:
        dead.add(a)
        _save_dead(dead)
        logger.info(f"[bounce_watch] marked dead: {a}")


def _is_bounce(msg: dict) -> bool:
    frm = (msg.get("from") or "").lower()
    subj = (msg.get("subject") or "").lower()
    if any(s in frm for s in _BOUNCE_SENDERS):
        return True
    return any(s in subj for s in _BOUNCE_SUBJECTS)


def _failed_recipients(msg: dict, own_address: str) -> list[str]:
    """Addresses named in a bounce body, excluding our own."""
    body = f"{msg.get('subject') or ''}\n{msg.get('text') or msg.get('body') or ''}"
    out = []
    for raw in _EMAIL_RE.findall(body):
        a = raw.strip().lower().rstrip(".,;:)>\"'")
        if a == own_address or any(s in a for s in _BOUNCE_SENDERS):
            continue
        if a.endswith(("googlemail.com", "google.com")):
            continue               # the notifier itself, not the failed target
        if a not in out:
            out.append(a)
    return out


def scan_bounces(hours: int = 48, limit: int = 40) -> dict:
    """Scan recent mail for delivery failures; record the addresses that failed.

    Never raises — a mail problem must not interrupt an apply cycle.
    """
    try:
        from agents.email_reader import load_gmail_config, recent_messages
        cfg = load_gmail_config()
        own = (cfg.get("address") or "").lower()
        if not own or not cfg.get("app_password"):
            return {"scanned": 0, "new_dead": 0, "reason": "gmail not configured"}

        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        messages = recent_messages(since, limit=limit)
        dead = load_dead()
        before = len(dead)
        for msg in messages:
            if not _is_bounce(msg):
                continue
            for addr in _failed_recipients(msg, own):
                dead.add(addr)
        if len(dead) != before:
            _save_dead(dead)
        return {"scanned": len(messages), "new_dead": len(dead) - before,
                "total_dead": len(dead)}
    except Exception as e:
        logger.warning(f"[bounce_watch] scan failed: {e}")
        return {"scanned": 0, "new_dead": 0, "error": str(e)}
