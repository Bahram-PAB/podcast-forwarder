"""Offline checks for the forwarder — no network, no secrets.

Run: python test_bot.py

The fakes below mirror the real Telethon shapes on purpose: msg.audio is a bare
Document whose duration lives on DocumentAttributeAudio. A plain object with a
.duration attribute would let the original crash bug pass.
"""
import inspect
import unittest

from telethon import utils
from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
    MessageMediaDocument,
    PeerChannel,
    PeerChat,
)

from bot import resolve_target


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
    def test_persian_digits_without_mixed_zero(self):
        from bot import format_caption
        self.assertEqual(format_caption("گوش‌واره", 900), "گوش‌واره\n۱۵:۰۰")
        self.assertEqual(format_caption("آذربایجان", 960), "آذربایجان\n۱۶:۰۰")
        self.assertEqual(format_caption("X", 3661), "X\n۶۱:۰۱")

    def test_no_ascii_digits(self):
        from bot import format_caption
        for secs in (905, 3599, 10800):
            self.assertFalse(
                any(c.isdigit() and c.isascii() for c in format_caption("نام", secs))
            )


class TestChannels(unittest.TestCase):
    def test_parses_all_six_channels(self):
        from bot import load_channels
        got = dict(load_channels())
        for handle in ("ahangify", "gooshvaaareh", "remixjavan_com",
                       "Azerbaijan20", "melody9", "savadnameh"):
            self.assertIn(handle, got)

    def test_named_channel_keeps_its_name(self):
        from bot import load_channels
        self.assertEqual(load_channels()[0][1], "آهنگیفای")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)