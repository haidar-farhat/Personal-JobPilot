"""Static invariants on the shipped extension source. No backend, no browser.

These guard the two ways this extension can hurt its user, both of which have
bitten us for real:

1. Self-reloading. A `getManifest().version !== SW_BUILD -> runtime.reload()`
   "self-heal" bricked the extension on 2026-08-03: reload() re-reads the same
   file with the same stale constant, so it loops until Chrome kills it with
   "This extension reloaded itself too frequently."
2. Auto-submitting an application. The whole promise is "we fill, YOU click
   Apply." A stray .submit() or a click on a submit control would fire off a
   half-filled application under the user's name.
"""

import re
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[1] / "browser-extension"
JS = sorted(EXT.glob("*.js")) + sorted(EXT.glob("content/*.js"))


def _src(p: Path) -> str:
    """Source with comments stripped, so prose about a banned call doesn't trip the check."""
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def test_js_files_are_present():
    """A rename that silently empties the file list would make every check below vacuous."""
    assert {p.name for p in JS} >= {"service_worker.js", "popup.js", "scan.js", "fill.js"}


@pytest.mark.parametrize("path", JS, ids=lambda p: p.name)
def test_never_reloads_itself(path):
    assert "runtime.reload" not in _src(path), (
        f"{path.name} calls chrome.runtime.reload(). A service worker that reloads on "
        f"startup cannot converge — Chrome disables the extension after 5 reloads in 10s. "
        f"Use the chrome://extensions reload button instead."
    )


@pytest.mark.parametrize("path", JS, ids=lambda p: p.name)
def test_never_submits_the_form(path):
    src = _src(path)
    # .submit() on a form, and clicking anything that is a submit control.
    assert not re.search(r"\.submit\s*\(", src), f"{path.name} calls .submit()"
    assert not re.search(r"""\[type=["']submit["']\][^\n]*\.click""", src), (
        f"{path.name} clicks a submit control"
    )
    assert not re.search(r"requestSubmit\s*\(", src), f"{path.name} calls requestSubmit()"


def test_manifest_version_matches_no_hardcoded_build_constant():
    """The bug was a hardcoded version constant drifting from the manifest.

    Nothing in the extension should hardcode its own version — read it from
    getManifest() at the point of use, or don't track it at all.
    """
    for path in JS:
        src = _src(path)
        assert not re.search(r"""^\s*const\s+SW_BUILD\s*=""", src, flags=re.M), (
            f"{path.name} reintroduced a hardcoded SW_BUILD constant"
        )
