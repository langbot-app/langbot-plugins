"""Console responses must stay attributed to the session that requested them.

Review finding F1: `openSession()` switched the shared page state immediately
while `/messages` was still in flight, so an out-of-order response could paint
session A's history, title and member card while `state.current` (the reply
target) was already B. The same attribution gap existed for the takeover
response and for the polling writeback, and a switch did not clear the previous
conversation or block sending.

The console is a browser page, so these tests run its real inline script inside
a fake DOM (tests/js/console_attribution_harness.mjs) with API responses that are
resolved manually, which makes "the older response lands last" deterministic.
Skipped when no `node` binary is available.

Run from HumanTakeover: python -m pytest tests
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).parent / "js" / "console_attribution_harness.mjs"
PAGE = PLUGIN_ROOT / "components" / "pages" / "console" / "index.html"


@pytest.fixture(scope="module")
def obs() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the console page script")
    assert PAGE.is_file(), f"missing console page: {PAGE}"
    proc = subprocess.run(
        [node, str(HARNESS), str(PAGE)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    marker = "__RESULT__"
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith(marker)),
        None,
    )
    if line is None:
        pytest.fail(
            f"harness produced no result (exit {proc.returncode})\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    data = json.loads(line[len(marker) :])
    broken = {name: value for name, value in data.items() if isinstance(value, dict) and "error" in value}
    assert not broken, f"harness scenarios crashed: {broken}"
    return data


def test_interleaved_loads_attribute_to_current_session(obs):
    """Opening A then B: B's response wins even though A's arrives last."""
    scenario = obs["interleaved_loads_and_reply"]
    after = scenario["afterLoads"]
    assert after["current"] == "B"
    assert after["displayedKey"] == "B", "the late A response overwrote B's conversation"
    assert after["currentKey"] == "B", "displayed data is not attributed to its session"
    assert after["titleKey"] == "name-B"


def test_reply_goes_to_the_displayed_session(obs):
    """The reply body's session_key must equal the displayed conversation's key."""
    scenario = obs["interleaved_loads_and_reply"]
    assert scenario["replySent"] is True
    assert scenario["replyKeyMatchesDisplayed"] is True, (
        "reply was sent to a different session than the one on screen "
        f"(displayed={scenario['displayedAtTyping']!r}, sent={scenario['replyKey']!r})"
    )
    assert scenario["replyKey"] == "B"
    assert scenario["displayedAtTyping"] == "B"


def test_switch_clears_previous_conversation_and_blocks_send(obs):
    """Switching to a still-loading session must clear A's state and block sending."""
    scenario = obs["switch_while_loading"]
    assert scenario["loaded"]["displayedKey"] == "A"

    while_loading = scenario["whileLoading"]
    assert while_loading["current"] == "B"
    assert while_loading["displayedKey"] is None, "A's data was still displayed after switching to B"
    assert while_loading["currentKey"] is None
    assert while_loading["titleKey"] == ""
    assert while_loading["messagesHtml"] == ""
    assert while_loading["infoHtml"] == ""
    assert while_loading["sendDisabled"] is True, "sending was allowed before B finished loading"
    assert while_loading["replySentWhileLoading"] is False, (
        "a reply typed against A was posted while B was not loaded yet"
    )

    after = scenario["afterLoad"]
    assert after["displayedKey"] == "B"
    assert after["sendDisabled"] is False


def test_poll_writeback_ignores_stale_snapshot(obs):
    """A poll snapshot for A must not repaint B when it lands after the switch."""
    scenario = obs["poll_stale_writeback"]
    assert scenario["polledKey"] == "A"
    after = scenario["after"]
    assert after["current"] == "B"
    assert after["displayedKey"] == "B"
    assert after["takeoverActive"] is False, "A's takeover snapshot was applied to B"
    assert after["buttonText"] == "接管"
    assert after["countdownShown"] is False


def test_takeover_response_ignores_stale_session(obs):
    """A takeover response for A must not repaint B's takeover UI."""
    scenario = obs["takeover_stale_response"]
    assert scenario["requestKey"] == "A"
    after = scenario["after"]
    assert after["current"] == "B"
    assert after["displayedKey"] == "B"
    assert after["takeoverActive"] is False, "A's takeover response was applied to B"
    assert after["buttonText"] == "接管"
    assert after["countdownShown"] is False


def test_current_session_still_applies_its_own_responses(obs):
    """Guard against over-blocking: the selected session's own responses still apply."""
    scenario = obs["current_session_still_works"]
    after_load = scenario["afterLoad"]
    assert after_load["displayedKey"] == "Z"
    assert after_load["takeoverActive"] is True
    assert after_load["buttonText"] == "取消接管"
    assert after_load["countdownShown"] is True

    assert scenario["replyKey"] == "Z"
    after_send = scenario["afterSend"]
    assert after_send["displayedKey"] == "Z"
    assert after_send["takeoverActive"] is False
    assert after_send["buttonText"] == "接管"
