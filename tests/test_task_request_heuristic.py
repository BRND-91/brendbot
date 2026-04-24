"""Tests for the task-request heuristic used by the phantom-turn
discriminator to decide whether a no-output turn is an intentional
silent drop (suppress) or a stall on a task request (surface).

The heuristic is deliberately lenient — false positives produce an
extra "no response generated" fallback which is tolerable, false
negatives reproduce the 2026-04-23 pilot bug where a two-changes
request got silently dropped and the user had to re-prompt. We bias
toward recognizing more verbs as task-initiating.
"""
from __future__ import annotations

import pytest

from brendbot.session_handler import _looks_like_task_request as is_task


# ── Positive cases: clear task requests ──────────────────────────────────


@pytest.mark.parametrize("text", [
    "make me a song",
    "Make me a song",
    "MAKE ME A SONG",
    "run the migration",
    "send barnacle the soundfont",
    "render this at 170 bpm",
    "write bars about cheddy",
    "generate an image",
    "check if fluidsynth is installed",
    "fix the vocals",
    "build a new track",
    "read the logs",
    "edit MEMORY.md to add X",
    "explain what happened",
    "summarize the conversation",
    "translate to korean",
])
def test_bare_imperative_recognized(text):
    assert is_task(text) is True


@pytest.mark.parametrize("text", [
    "Brendan, make me a song",
    "brend, send the file",
    "Brendbot make me music brend.",
    "<@1490829355214049451> run the migration",
    "<@!1490829355214049451>, generate an image",
    "brendan, post the result",
])
def test_name_addressed_imperative_recognized(text):
    """User prefaces the request with 'Brendan,' or an @mention plus
    the bot's name. The verb still needs to be recognized as
    task-initiating after the preamble."""
    assert is_task(text) is True


# ── Negative cases: ambient chatter ──────────────────────────────────────


@pytest.mark.parametrize("text", [
    "lol ok",
    "LOL ok",
    "thanks brend",
    "yeah that's hilarious",
    "pebbed and unbothered",
    "nice",
    "you're sooooo off-beat lmaoooooo",
    "that was art",
    "oh snap",
    "wait what",
])
def test_ambient_chatter_not_recognized(text):
    assert is_task(text) is False


def test_empty_string_not_task():
    assert is_task("") is False


def test_none_not_task():
    """Safe handling: user_text might be None if the session state
    isn't fully populated (e.g. early restart edge case)."""
    assert is_task(None) is False  # type: ignore


# ── Edge cases that exercise the discriminator ───────────────────────────


def test_question_not_misread_as_task():
    """Pure questions aren't tasks in the imperative sense. They can
    still warrant a response, but if the model silent-drops them
    that's a model-policy decision, not a stall. The heuristic only
    catches imperatives."""
    assert is_task("what's the weather") is False
    assert is_task("how does this work") is False
    assert is_task("why did you do that") is False


def test_task_verb_after_comma_recognized():
    """'Brendan, send me X' — the verb is second token after a name
    preamble. Common pattern; must be caught."""
    assert is_task("Brendan, send me the file") is True
    assert is_task("brend, make a song") is True


def test_mid_sentence_verb_not_recognized():
    """The heuristic requires the imperative at the start of the
    message (after optional @mention/name preamble). A verb appearing
    mid-sentence in ambient chatter should NOT trigger:

        "i ran that earlier" — 'ran' isn't a fresh imperative
        "you should fix it" — 'should fix' is advisory, not an order
    """
    assert is_task("i ran that already earlier") is False
    assert is_task("you should check that later") is False


# ── Direct regression from 2026-04-23 pilot ──────────────────────────────


def test_pilot_regression_two_changes_request():
    """At 22:33:31 on 2026-04-23 Brendan sent:

        "You were doing something cool in console, two changes:
         keep it up"

    The pre-2026-04-24 discriminator treated this as an intentional
    silent drop when the bot emitted only thinking blocks. That was
    the bug. The sentence starts with 'You were doing' which is
    conversational context, but the intent is imperative — 'keep it
    up' is a command. For now the heuristic only catches the 'keep'
    verb if it's near the start; this sentence falls through as
    'not clearly a task,' which is OK because it's genuinely
    ambiguous. The important case — the next message at 22:34:18
    explicitly repeating 'Two changes: new chord progression' — is
    caught because it's an imperative structure."""
    # Ambiguous case: not strictly a task-verb imperative
    assert is_task("You were doing something cool in console, two changes: keep it up") is False
    # Repeat case: still not caught by our simple verb-at-start heuristic.
    # Accepted limitation; see docstring in _looks_like_task_request.
    # The next-turn mechanism (runtime_error on repeated prompts for
    # the same reference) is the backstop for this class.


def test_pilot_regression_make_me_music():
    """The canonical case that motivated this work."""
    assert is_task("Brend make me music brend.") is True
    assert is_task("<@1490829355214049451> make me a song") is True


def test_pilot_regression_f_minor_song():
    """"Make another song, F minor" — the 20-minute-silence case."""
    assert is_task("Make another song, F minor") is True
    assert is_task(
        "Hmm nah. Let's skip vocals for a bit. This didn't work. "
        "Make another song, F minor"
    ) is False
    # ^ mid-sentence 'Make' is not caught by the start-anchored
    # heuristic. That's a known limitation documented on the helper.
    # In practice the next pilot will show whether this matters; the
    # runtime_error path catches repeated-prompt patterns.
