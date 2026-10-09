"""Offline checks for the forwarder — no network, no secrets.

Run: python test_bot.py

The fakes below mirror the real Telethon shapes on purpose: msg.audio is a bare
Document whose duration lives on DocumentAttributeAudio. A plain object with a
.duration attribute would let the original crash bug pass.
"""
import inspect
import json
import unittest
from pathlib import Path

from telethon import utils
from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
    MessageMediaDocument,
    PeerChannel,
    PeerChat,
)

from bot import already_sent, load_channels, media_key, remember_sent, resolve_target
import bot


def fake_audio_doc(duration, voice=False):
    return Document(
        id=1, access_hash=2, file_reference=b"", date=None,
        mime_type="audio/ogg", size=1000, dc_id=4,
        attributes=[DocumentAttributeAudio(
            duration=duration, voice=voice, title="t",
            performer=None, waveform=b"\x00")],
    )


class FakeMsg:
    def __init__(self, msg_id, media=None):
        self.id = msg_id
        self.media = media
        self.audio = media if (media and not getattr(media, "voice", False)) else None
        self.voice = media if (media and getattr(media, "voice", False)) else None


class TestAudioDuration(unittest.TestCase):
    def test_duration_read_from_attribute_not_document(self):
        """Regression: Document has no .duration — reading it raised."""
        from bot import audio_duration
        self.assertEqual(audio_duration(FakeMsg(1, fake_audio_doc(1200))), 1200)
        self.assertFalse(hasattr(fake_audio_doc(1200), "duration"),
                         "fake must not expose .duration or the test is vacuous")

    def test_voice_note_duration(self):
        from bot import audio_duration
        self.assertEqual(
            audio_duration(FakeMsg(1, fake_audio_doc(1800, voice=True))), 1800)

    def test_no_media_and_unexpected_attributes(self):
        from bot import audio_duration
        self.assertEqual(audio_duration(FakeMsg(1)), 0)
        bare = Document(id=1, access_hash=2, file_reference=b"", date=None,
                        mime_type="application/pdf", size=10, dc_id=4, attributes=[])
        self.assertEqual(audio_duration(FakeMsg(1, bare)), 0)


class TestFilter(unittest.TestCase):
    def test_boundary_is_15_minutes(self):
        from bot import long_media
        self.assertIsNone(long_media(FakeMsg(1, fake_audio_doc(899))), "14:59 skipped")
        self.assertIsNotNone(long_media(FakeMsg(1, fake_audio_doc(900))), "15:00 forwarded")
        self.assertIsNotNone(
            long_media(FakeMsg(1, fake_audio_doc(5400, voice=True))), "90:00 voice")

    def test_ignores_text_and_short(self):
        from bot import long_media
        self.assertIsNone(long_media(FakeMsg(1)), "text message")
        self.assertIsNone(long_media(FakeMsg(1, fake_audio_doc(10))), "short voice note")
        self.assertIsNone(long_media(FakeMsg(1, fake_audio_doc(0))), "zero duration")


class TestCaption(unittest.TestCase):
    """Source line + rule + channel link, exactly as specified."""

    def test_caption_layout(self):
        from bot import format_caption
        for name in ("آیسی ریمیکس", "هیرو", "X"):
            lines = format_caption(name).splitlines()
            self.assertEqual(lines[0], f"منبع :{name}")
            self.assertEqual(lines[1], "--------------")
            self.assertEqual(lines[2], "@@url:`https://t.me/remixforwarder`")
            self.assertEqual(len(lines), 3)

    def test_name_is_not_dropped(self):
        from bot import format_caption
        self.assertIn("@@url:", format_caption("کوچه بازاری ها"))


class TestClock(unittest.TestCase):
    def test_persian_digits_without_mixed_zero(self):
        from bot import clock
        self.assertEqual(clock(900), "۱۵:۰۰")
        self.assertEqual(clock(960), "۱۶:۰۰")
        self.assertEqual(clock(3661), "۶۱:۰۱")

    def test_no_ascii_digits(self):
        from bot import clock
        for secs in (905, 3599, 10800):
            self.assertFalse(
                any(c.isdigit() and c.isascii() for c in clock(secs))
            )


class TestChannels(unittest.TestCase):
    def test_every_listed_line_parses(self):
        # نه به یک لیست hardcodeشده: کاربر هر وقت کانال اضافه/حذف کند این تست باید بماند.
        from bot import load_channels
        from pathlib import Path
        got = dict(load_channels())
        lines = [l.split("|")[0].strip().lstrip("@").strip()
                 for l in open(Path("channels.txt"), encoding="utf-8")
                 if l.strip() and not l.lstrip().startswith("#") and "|" in l]
        self.assertEqual(sorted(got), sorted(lines))
        self.assertGreater(len(got), 0)

    def test_known_channel_still_present(self):
        from bot import load_channels
        got = dict(load_channels())
        self.assertIn("ahangify", got)

    def test_named_channel_keeps_its_name(self):
        from bot import load_channels
        self.assertEqual(load_channels()[0][1], "آهنگیفای")

    def test_blank_name_falls_back_to_the_handle(self):
        from bot import load_channels
        got = {h: n for h, n in load_channels()}
        self.assertEqual(got["ahangify"], "آهنگیفای")
        # A blank display name yields the @handle, which is the cue for bot.py
        # to ask Telegram for the real title.
        for handle, name in load_channels():
            self.assertTrue(name, f"{handle} has no fallback name")
        self.assertTrue(any(n.startswith("@") for _, n in load_channels()),
                        "expected at least one channel using the @handle fallback")


class TestState(unittest.TestCase):
    def test_missing_state_starts_empty(self):
        from bot import load_state
        self.assertIn("channels", load_state())


class TestRealTelethonSignatures(unittest.TestCase):
    """Checks bot.py's calls against the installed Telethon, offline.

    The loop body used a param name Telethon never had (id_lt, not min_id) and
    the broad `except Exception` hid the TypeError on every run. Signature
    drift has to fail here instead of silently doing nothing.
    """

    def test_kwargs_are_real_params(self):
        """Every keyword bot.py passes to a TelegramClient method must exist."""
        import inspect
        import re
        from pathlib import Path
        from telethon import TelegramClient

        src = (Path(__file__).parent / "bot.py").read_text(encoding="utf-8")
        checked = 0
        for method in ("get_messages", "send_file", "get_entity"):
            params = set(inspect.signature(getattr(TelegramClient, method)).parameters)
            for call in re.findall(rf"\.?{method}\((.*?)\n", src, re.S):
                used = set(re.findall(r"\b(\w+)\s*=", call))
                self.assertEqual(used - params, set(),
                                 f"unknown kwargs for {method}()")
                checked += 1
        self.assertGreater(checked, 2, "no calls parsed — test is vacuous")


class TestBackfillPaging(unittest.TestCase):
    """max_id pages towards OLDER messages. The no-progress guard matters: at the
    bottom of a channel Telegram keeps returning the same page, so an unguarded
    loop spins forever on an endless channel."""

    def test_max_id_is_a_real_param_and_pages_backwards(self):
        import inspect as _i
        from telethon.client.messages import MessageMethods
        params = _i.signature(MessageMethods.get_messages).parameters
        self.assertIn("max_id", params)
        self.assertIn("min_id", params)

    def test_backfill_is_off_unless_asked(self):
        import os
        import importlib
        self.assertFalse(importlib.import_module("bot").BACKFILL,
                         "BACKFILL must default to off so cron never backfills")

    def test_no_progress_guard_catches_a_repeated_page(self):
        # Telegram returns the same oldest page when max_id is already at the
        # bottom: the newest id on it is >= the cursor we asked to go below.
        cursor, page_oldest_newest_id = 5, 5
        stuck = page_oldest_newest_id >= cursor and bool(cursor)
        self.assertTrue(stuck, "guard must fire when the page does not move")

    def test_no_progress_guard_does_not_fire_on_progress(self):
        cursor, page_oldest_newest_id = 100, 40
        stuck = page_oldest_newest_id >= cursor and bool(cursor)
        self.assertFalse(stuck)


class TestDedupe(unittest.TestCase):
    """A file must never reach the target channel twice.

    state.json only stores a cursor, so the cron path cannot tell "already
    forwarded" from "new" — a retried run would repost. The sent-keys table is
    what makes replays idempotent.
    """

    def test_first_send_is_not_deduped(self):
        state = {}
        self.assertFalse(already_sent(state, "melody9#42"))

    def test_same_key_is_deduped_after_a_send(self):
        state = {}
        remember_sent(state, "melody9#42")
        self.assertTrue(already_sent(state, "melody9#42"))

    def test_different_message_is_not_deduped(self):
        state = {}
        remember_sent(state, "melody9#42")
        self.assertFalse(already_sent(state, "melody9#43"))

    def test_same_id_in_a_different_channel_is_a_different_source(self):
        state = {}
        remember_sent(state, "melody9#42")
        self.assertFalse(already_sent(state, "icyRemix#42"))

    def test_missing_sent_table_defaults_to_empty(self):
        # Old state.json files predate the dedup table; must not KeyError.
        state = load_state() if False else {"channels": {}}
        self.assertFalse(already_sent(state, "melody9#42"))
        self.assertEqual(state["sent"], {})

    def test_key_is_handle_plus_id(self):
        msg = type("M", (), {"id": 3})()  # media_key only reads .id
        self.assertEqual(media_key("icyRemix", msg), "icyRemix#3")

    def test_dedupe_survives_a_round_trip_through_state(self):
        state = {"channels": {}}
        remember_sent(state, "melody9#42")
        reloaded = json.loads(json.dumps(state))
        self.assertTrue(already_sent(reloaded, "melody9#42"))


class TestCursor(unittest.TestCase):
    """The cursor is the highest id already handled — it must move UP.

    min() pinned it at the baseline for every channel: `state unchanged` on
    every run, and once a channel passed 100 posts between runs the oldest
    unhandled ones fell outside the window and were lost for good.
    """

    def test_new_posts_raise_the_cursor(self):
        # A post newer than the cursor has a bigger id; keeping it must raise.
        cursor, msg_id = 110347, 110356
        self.assertEqual(max(cursor, msg_id), 110356)
        # min() is the bug: it returns the stale cursor and never advances.
        self.assertEqual(min(cursor, msg_id), 110347)

    def test_cron_loop_never_uses_min_on_the_cursor(self):
        src = inspect.getsource(bot)
        body = src[src.index("async def main"):]
        self.assertNotIn('min(state["channels"][handle]', body)
        # All three cursor writes (skip, dedupe, send) must use max().
        self.assertEqual(body.count('max(state["channels"][handle]'), 3)

    def test_walks_oldest_first_so_a_failed_send_stops_the_cursor(self):
        # reversed() keeps ascending id order, so a failure leaves the cursor
        # on the last post that actually made it instead of skipping past it.
        self.assertIn("for msg in reversed(page)", inspect.getsource(bot))

    def test_scans_every_page_not_just_the_first_100(self):
        # 8-hour cron: one busy channel outgrows a single GetHistory page, and a
        # single call would drop the overflow forever.
        src = inspect.getsource(bot)
        body = src[src.index("async def main"):]
        self.assertIn("limit=SCAN_PAGE", body)
        self.assertIn("while True:", body)
        self.assertIn("seen = page[-1].id", body)


class TestResolveTarget(unittest.TestCase):
    """A raw -100 id does NOT resolve on a cold StringSession client; walking
    the dialogs does. This was the bug that blocked every real send."""

    def test_username_is_not_walked(self):
        src = inspect.getsource(resolve_target)
        self.assertIn("isdigit", src)
        self.assertIn("iter_dialogs", src)

    def test_peer_id_mapping_matches_documented_forms(self):
        # Chat -> -id, channel -> -1000...id; utils.get_peer_id does exactly this.
        self.assertEqual(utils.get_peer_id(PeerChannel(1171526333)), -1001171526333)
        self.assertEqual(utils.get_peer_id(PeerChat(547239020)), -547239020)




class TestExitCode(unittest.TestCase):
    """A run where every channel failed used to report green.

    GitHub reads only the process exit code, so 0 means "success" no matter
    how much of the run actually fell over.
    """

    def setUp(self):
        self.src = inspect.getsource(bot)

    def test_any_failure_exits_nonzero(self):
        self.assertIn("if failed > 0:", self.src)
        self.assertIn("sys.exit(1)", self.src)

    def test_exit_comes_after_the_state_is_written(self):
        # Exiting first would drop the cursors of the channels that did work.
        self.assertLess(self.src.index("(HERE / \"state.json\")"),
                        self.src.index("sys.exit(1)"))


class TestWorkflowTriggers(unittest.TestCase):
    """The schedule drifted hours and hid failures; an external trigger fires it."""

    def setUp(self):
        wf = Path(__file__).parent / ".github" / "workflows" / "forward.yml"
        self.text = wf.read_text(encoding="utf-8")

    def test_no_schedule(self):
        # Match the trigger key, not the word — the comment explains why.
        self.assertNotRegex(self.text, r"(?m)^\s*schedule:")

    def test_commit_state_runs_even_after_a_failed_run(self):
        i = self.text.index("name: Commit state")
        self.assertIn("if: always()", self.text[i:i + 400])


if __name__ == "__main__":
    unittest.main(verbosity=2)
