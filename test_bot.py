"""Offline checks for the forwarder — no network, no secrets.

Run: python test_bot.py
"""
import unittest


class FakeMedia:
    def __init__(self, duration):
        self.duration = duration


class FakeMsg:
    def __init__(self, msg_id, audio=None, voice=None):
        self.id = msg_id
        self.audio = audio
        self.voice = voice


class TestFilter(unittest.TestCase):
    def test_boundary_is_15_minutes(self):
        from bot import long_media
        self.assertIsNone(long_media(FakeMsg(1, audio=FakeMedia(899))), "14:59 skipped")
        self.assertIsNotNone(long_media(FakeMsg(1, audio=FakeMedia(900))), "15:00 forwarded")
        self.assertIsNotNone(long_media(FakeMsg(1, voice=FakeMedia(5400))), "90:00 forwarded")

    def test_ignores_text_and_short(self):
        from bot import long_media
        self.assertIsNone(long_media(FakeMsg(1)), "text message")
        self.assertIsNone(long_media(FakeMsg(1, voice=FakeMedia(10))), "short voice note")
        self.assertIsNone(long_media(FakeMsg(1, audio=FakeMedia(0))), "missing duration")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)