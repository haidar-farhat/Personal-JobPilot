"""Mailbox verification and deep crawl — the safety properties, locked in.

The SMTP probe is the only code in this project that opens a connection to a
stranger's mail server, so its three correctness rules are not style choices:

  1. CATCH-ALL IS DETECTED FIRST. Microsoft 365 and Google Workspace accept
     every RCPT TO. On such a domain a "valid" verdict on the real address is
     meaningless, and acting on it would be worse than not verifying at all —
     it would launder a guess into a certainty.
  2. A 4xx IS "unknown", NEVER "invalid". Greylisting ("try again later") is
     extremely common on first contact. Treating it as invalid would silently
     discard good addresses and mark them dead forever.
  3. IT NEVER SENDS DATA. The probe establishes existence and hangs up. If a
     DATA command ever appeared, this module would be sending unsolicited mail
     from a code path whose whole purpose is to avoid doing that.

Every test here is offline: the SMTP class and the HTTP session are stubbed, and
an unstubbed call is a hard failure rather than a silent network hit.
"""

from __future__ import annotations

import smtplib

import pytest

import utils.mailbox_probe as M


# --------------------------------------------------------------------------
# offline guarantees
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any real socket or HTTP call fails loudly instead of leaving the machine."""
    def _boom(*a, **k):                       # pragma: no cover - guard
        raise AssertionError("test attempted a real network call")
    monkeypatch.setattr(M, "_SMTP_CLASS", _boom)
    monkeypatch.setattr(M.requests.Session, "get", _boom, raising=False)
    monkeypatch.setattr(M, "mx_hosts", lambda domain: ["mx.example.com"])
    M.clear_probe_cache()
    yield
    M.clear_probe_cache()


class FakeSMTP:
    """Records the conversation so a test can assert what was and wasn't said."""

    #: rcpt code by recipient predicate, set per-test
    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.commands: list[tuple[str, str]] = []
        self.ehlo_code = 250
        self.mail_code = 250
        self.decoy_code = 550        # server distinguishes mailboxes by default
        self.real_code = 250
        self.raise_on = None

    # -- SMTP surface used by _probe ---------------------------------------
    def ehlo(self):
        self.commands.append(("EHLO", ""))
        return self.ehlo_code, b"hello"

    def helo(self):
        self.commands.append(("HELO", ""))
        return self.ehlo_code, b"hello"

    def mail(self, sender):
        self.commands.append(("MAIL", sender))
        return self.mail_code, b"ok"

    def rcpt(self, addr):
        self.commands.append(("RCPT", addr))
        if self.raise_on == "rcpt":
            raise smtplib.SMTPServerDisconnected("dropped")
        # the decoy is the randomised jp-...-probe@ local part
        if addr.startswith("jp-") and "-probe@" in addr:
            return self.decoy_code, b"no such user"
        return self.real_code, b"ok"

    def data(self, *a, **k):          # pragma: no cover - must never be called
        self.commands.append(("DATA", ""))
        raise AssertionError("the probe must never send DATA")

    def send_message(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("the probe must never send a message")

    def quit(self):
        self.commands.append(("QUIT", ""))

    def close(self):
        pass


@pytest.fixture
def smtp(monkeypatch):
    """Install FakeSMTP and hand the instance back for assertions."""
    made: list[FakeSMTP] = []

    def factory(host, port, timeout=None):
        s = FakeSMTP(host, port, timeout)
        if made and hasattr(made[0], "_template"):
            pass
        made.append(s)
        return s

    monkeypatch.setattr(M, "_SMTP_CLASS", factory)
    return made


def _configure(monkeypatch, *, decoy=550, real=250, ehlo=250, mail=250):
    """Install an SMTP whose codes are fixed before the connection is made."""
    made: list[FakeSMTP] = []

    def factory(host, port, timeout=None):
        s = FakeSMTP(host, port, timeout)
        s.decoy_code, s.real_code, s.ehlo_code, s.mail_code = decoy, real, ehlo, mail
        made.append(s)
        return s

    monkeypatch.setattr(M, "_SMTP_CLASS", factory)
    return made


# --------------------------------------------------------------------------
# 1. catch-all
# --------------------------------------------------------------------------

def test_catch_all_domain_never_reports_valid(monkeypatch):
    """A server that accepts a random local part proves nothing about the real one."""
    made = _configure(monkeypatch, decoy=250, real=250)
    r = M.verify_mailbox("careers@acme.com")
    assert r["status"] == "catch_all"
    assert r["status"] != "valid", "a catch-all must never be laundered into 'valid'"
    assert "accepted a random local part" in r["detail"]
    # and it stopped before bothering to ask about the real address
    rcpts = [a for c, a in made[0].commands if c == "RCPT"]
    assert len(rcpts) == 1 and rcpts[0].startswith("jp-")


def test_decoy_is_probed_before_the_real_address(monkeypatch):
    """Order matters: asking about the real address first would waste the signal."""
    made = _configure(monkeypatch, decoy=550, real=250)
    M.verify_mailbox("careers@acme.com")
    rcpts = [a for c, a in made[0].commands if c == "RCPT"]
    assert rcpts[0].startswith("jp-") and "-probe@acme.com" in rcpts[0]
    assert rcpts[1] == "careers@acme.com"


def test_decoy_local_part_is_randomised(monkeypatch):
    """A fixed decoy would be learnable, and a server could whitelist it."""
    _configure(monkeypatch, decoy=550, real=250)
    a = M._catch_all_probe_address("acme.com")
    b = M._catch_all_probe_address("acme.com")
    assert a != b
    assert a.endswith("@acme.com")


def test_a_real_mailbox_on_a_discriminating_server_is_valid(monkeypatch):
    made = _configure(monkeypatch, decoy=550, real=250)
    r = M.verify_mailbox("careers@acme.com")
    assert r["status"] == "valid"
    assert r["mx"] == "mx.example.com"
    assert len([a for c, a in made[0].commands if c == "RCPT"]) == 2


def test_a_missing_mailbox_is_invalid(monkeypatch):
    _configure(monkeypatch, decoy=550, real=550)
    assert M.verify_mailbox("nosuchbox@acme.com")["status"] == "invalid"


# --------------------------------------------------------------------------
# 2. temporary failures are never "invalid"
# --------------------------------------------------------------------------

@pytest.mark.parametrize("code", [421, 450, 451, 452])
def test_greylisting_on_the_real_address_is_unknown(monkeypatch, code):
    _configure(monkeypatch, decoy=550, real=code)
    r = M.verify_mailbox("careers@acme.com")
    assert r["status"] == "unknown", f"{code} must not be read as a missing mailbox"


@pytest.mark.parametrize("code", [421, 450, 451])
def test_greylisting_on_the_decoy_is_unknown(monkeypatch, code):
    """If the decoy is throttled the real address would be too — no signal."""
    _configure(monkeypatch, decoy=code, real=250)
    r = M.verify_mailbox("careers@acme.com")
    assert r["status"] == "unknown"
    assert "inconclusive" in r["detail"]


def test_classify_boundaries():
    assert M._classify(250) == "valid"
    assert M._classify(450) == "unknown"
    assert M._classify(550) == "invalid"
    assert M._classify(None) == "unknown"


def test_a_dropped_connection_is_unknown_not_invalid(monkeypatch):
    def factory(host, port, timeout=None):
        s = FakeSMTP(host, port, timeout)
        s.raise_on = "rcpt"
        return s
    monkeypatch.setattr(M, "_SMTP_CLASS", factory)
    r = M.verify_mailbox("careers@acme.com")
    assert r["status"] == "unknown"


def test_a_refused_connection_is_unknown(monkeypatch):
    def factory(host, port, timeout=None):
        raise OSError("port 25 blocked by the local network")
    monkeypatch.setattr(M, "_SMTP_CLASS", factory)
    r = M.verify_mailbox("careers@acme.com")
    assert r["status"] == "unknown"
    assert r["code"] is None


def test_no_mx_is_unknown_not_invalid(monkeypatch):
    monkeypatch.setattr(M, "mx_hosts", lambda domain: [])
    assert M.verify_mailbox("careers@nowhere.example")["status"] in ("unknown", "invalid")


def test_a_refused_greeting_is_unknown(monkeypatch):
    _configure(monkeypatch, ehlo=554)
    assert M.verify_mailbox("careers@acme.com")["status"] == "unknown"


def test_a_refused_null_sender_is_unknown(monkeypatch):
    _configure(monkeypatch, mail=550)
    r = M.verify_mailbox("careers@acme.com")
    assert r["status"] == "unknown"
    assert "MAIL FROM" in r["detail"]


# --------------------------------------------------------------------------
# 3. it never sends mail
# --------------------------------------------------------------------------

def test_the_conversation_contains_no_data_command(monkeypatch):
    made = _configure(monkeypatch, decoy=550, real=250)
    M.verify_mailbox("careers@acme.com")
    verbs = [c for c, _ in made[0].commands]
    assert "DATA" not in verbs, "the probe must never send DATA"
    assert verbs[0] in ("EHLO", "HELO")
    assert "QUIT" in verbs


def test_the_connection_is_always_closed(monkeypatch):
    made = _configure(monkeypatch, decoy=550, real=550)
    M.verify_mailbox("careers@acme.com")
    assert ("QUIT", "") in made[0].commands


def test_probe_never_raises_whatever_the_server_does(monkeypatch):
    for exc in (smtplib.SMTPException("x"), OSError("x"), ValueError("x")):
        def factory(host, port, timeout=None, _e=exc):
            raise _e
        monkeypatch.setattr(M, "_SMTP_CLASS", factory)
        assert M.verify_mailbox("a@b.com")["status"] == "unknown"


# --------------------------------------------------------------------------
# caching and shape
# --------------------------------------------------------------------------

def test_result_shape_is_stable(monkeypatch):
    _configure(monkeypatch, decoy=550, real=250)
    r = M.verify_mailbox("careers@acme.com")
    assert set(r) >= {"status", "code", "detail", "mx"}


def test_a_second_call_is_served_from_cache(monkeypatch):
    made = _configure(monkeypatch, decoy=550, real=250)
    M.verify_mailbox("careers@acme.com")
    M.verify_mailbox("careers@acme.com")
    assert len(made) == 1, "probing the same address twice is what gets a prober blocked"


def test_refresh_bypasses_the_cache(monkeypatch):
    made = _configure(monkeypatch, decoy=550, real=250)
    M.verify_mailbox("careers@acme.com")
    M.verify_mailbox("careers@acme.com", refresh=True)
    assert len(made) == 2


def test_a_malformed_address_is_not_probed(monkeypatch):
    def factory(*a, **k):             # pragma: no cover - must not run
        raise AssertionError("should not connect for a malformed address")
    monkeypatch.setattr(M, "_SMTP_CLASS", factory)
    assert M.verify_mailbox("not-an-address")["status"] in ("unknown", "invalid")


# --------------------------------------------------------------------------
# deep crawl
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status
        self.headers = {"Content-Type": "text/html"}


def _pages(monkeypatch, mapping, *, robots=""):
    """Serve a fixed {path: html} map; anything else 404s."""
    seen: list[str] = []

    class S:
        headers: dict = {}

        def get(self, url, **k):
            seen.append(url)
            if url.endswith("/robots.txt"):
                return FakeResponse(robots)
            for path, html in mapping.items():
                if url.endswith(path):
                    return FakeResponse(html)
            return FakeResponse("", 404)

    monkeypatch.setattr(M, "_session", lambda session=None: S())
    return seen


def test_crawl_finds_an_address_on_a_standard_path(monkeypatch):
    _pages(monkeypatch, {"/careers": "<a href='mailto:careers@acme.com'>Careers</a>"})
    hits = M.deep_crawl_for_email("acme.com")
    assert any(a == "careers@acme.com" for a, _url in hits)


def test_crawl_follows_a_promising_link_to_a_nonstandard_path(monkeypatch):
    _pages(monkeypatch, {
        "/join-the-crew": "Email <a href='mailto:hiring@acme.com'>hiring@acme.com</a>",
        "acme.com": "<a href='/join-the-crew'>Work with us</a>",
    })
    hits = M.deep_crawl_for_email("acme.com")
    assert any(a == "hiring@acme.com" for a, _url in hits), \
        "a careers page on a non-standard path must still be reachable"


def test_crawl_never_returns_a_blocked_local_part(monkeypatch):
    _pages(monkeypatch, {"/contact": "mailto:accommodations@acme.com "
                                     "mailto:legal@acme.com"})
    assert M.deep_crawl_for_email("acme.com") == []


def test_crawl_prefers_a_recruiting_inbox(monkeypatch):
    _pages(monkeypatch, {"/contact": "mailto:hello@acme.com mailto:careers@acme.com"})
    best = M.deep_crawl_best("acme.com")
    assert best is not None and best[0] == "careers@acme.com"


def test_crawl_respects_the_request_budget(monkeypatch):
    seen = _pages(monkeypatch, {})
    M.deep_crawl_for_email("acme.com", budget=3)
    non_robots = [u for u in seen if not u.endswith("/robots.txt")]
    assert len(non_robots) <= 3


def test_crawl_obeys_robots_disallow(monkeypatch):
    seen = _pages(monkeypatch,
                  {"/careers": "mailto:careers@acme.com"},
                  robots="User-agent: *\nDisallow: /careers\n")
    M.deep_crawl_for_email("acme.com")
    assert not any(u.endswith("/careers") for u in seen)


def test_crawl_stays_on_the_one_host(monkeypatch):
    seen = _pages(monkeypatch, {
        "acme.com": "<a href='https://elsewhere.example/careers'>Careers</a>"})
    M.deep_crawl_for_email("acme.com")
    assert not any("elsewhere.example" in u for u in seen)


def test_crawl_returns_the_page_each_address_came_from(monkeypatch):
    _pages(monkeypatch, {"/careers": "mailto:careers@acme.com"})
    hits = M.deep_crawl_for_email("acme.com")
    assert hits and hits[0][1].endswith("/careers")


def test_crawl_survives_a_dead_site(monkeypatch):
    class S:
        headers: dict = {}

        def get(self, url, **k):
            raise M.requests.ConnectionError("connection refused")

    monkeypatch.setattr(M, "_session", lambda session=None: S())
    assert M.deep_crawl_for_email("acme.com") == []
