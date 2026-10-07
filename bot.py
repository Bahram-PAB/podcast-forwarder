# Forward long audio (>15 min) from Telegram channels to one group.
# A user session is mandatory: bots only see channels they are admin in, so
# channels we don't own are unreadable via the Bot API.
import asyncio
import json
import os
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

MIN_SECONDS = 900
HERE = Path(__file__).resolve().parent
SEND_GAP = 2  # seconds between forwards (flood safety)


def fa_digits(value):
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def long_media(msg):
    """audio = music/podcast file, voice = voice note; both carry duration."""
    media = getattr(msg, "audio", None) or getattr(msg, "voice", None)
    if media is not None and (media.duration or 0) >= MIN_SECONDS:
        return media
    return None


def format_caption(name, seconds):
    # Pad zeros in ASCII first, then convert — padding after gives "0۸".
    clock = f"{str(seconds // 60).zfill(2)}:{str(seconds % 60).zfill(2)}"
    return f"{name}\n{fa_digits(clock)}"


def load_channels():
    """'@handle | نام نمایشی' per line. Doubles as the channel checklist."""
    out = []
    for line in (HERE / "channels.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        handle, _, name = line.partition("|")
        handle = handle.strip().lstrip("@")
        out.append((handle, name.strip() or f"@{handle}"))
    return out


def load_state():
    try:
        return json.loads((HERE / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return {"channels": {}}


async def main():
    session = os.environ.get("TELEGRAM_SESSION", "")
    api_id = os.environ.get("TELETHON_API_ID", "")
    api_hash = os.environ.get("TELETHON_API_HASH", "")
    target = os.environ.get("TARGET_CHAT_ID", "")
    missing = [n for n, v in [
        ("TELEGRAM_SESSION", session), ("TELETHON_API_ID", api_id),
        ("TELETHON_API_HASH", api_hash), ("TARGET_CHAT_ID", target),
    ] if not v]
    if missing:
        sys.exit(f"missing env: {', '.join(missing)}")

    client = TelegramClient(StringSession(session), int(api_id), api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("session not authorized — regenerate TELEGRAM_SESSION")

    state = load_state()
    forwarded = failed = 0

    for handle, name in load_channels():
        try:
            entity = await client.get_entity(handle)
        except Exception as e:
            print(f"⚠️ {handle}: resolve failed ({type(e).__name__}), skipped")
            failed += 1
            continue

        if handle not in state["channels"]:
            # First sight of this channel: baseline only, older posts are not wanted.
            newest = await client.get_messages(entity, limit=1)
            if newest:
                state["channels"][handle] = newest[0].id
            print(f"⚪ {name}: baseline set, no history forwarded")
            continue

        seen = state["channels"][handle]
        try:
            # id_lt=seen returns exactly the unseen posts, newest first.
            msgs = await client.get_messages(entity, limit=100, id_lt=seen)
            for msg in msgs:
                state["channels"][handle] = min(state["channels"][handle], msg.id)

                media = long_media(msg)
                if media is None:
                    continue
                # Sending the media reference copies server-side: no download,
                # no 50 MB cap, and the session account must be in the group.
                await client.send_file(
                    target, msg.media, caption=format_caption(name, media.duration)
                )
                forwarded += 1
                clock = format_caption("", media.duration).splitlines()[1]
                print(f"✅ {name} — {clock}")
                await asyncio.sleep(SEND_GAP)

            if msgs:
                print(f"   {name}: {len(msgs)} new post(s)")
        except FloodWaitError as e:
            # One rate-limited channel must not kill the whole run.
            failed += 1
            print(f"⚠️ {handle}: flood wait {e.seconds}s, skipped")
        except Exception as e:
            failed += 1
            print(f"⚠️ {handle}: {type(e).__name__}: {e}")

    (HERE / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"forwarded={forwarded} failed={failed}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())