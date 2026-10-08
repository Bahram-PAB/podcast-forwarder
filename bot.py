# Forward long audio (>15 min) from Telegram channels to one group.
# A user session is mandatory: bots only see channels they are admin in, so
# channels we don't own are unreadable via the Bot API.
import asyncio
import json
import os
import sys
from pathlib import Path

from telethon import TelegramClient, errors, utils
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeAudio

MIN_SECONDS = 900
# Telegram answers GetHistory with at most 100 messages; more than that needs
# another call. The cron gap is 8 hours now, so one busy channel can outgrow a
# single page and the cursor would never see the overflow.
SCAN_PAGE = 100
HERE = Path(__file__).resolve().parent
SEND_GAP = 2  # seconds between forwards (flood safety)
# Backfill mode: forward every long file already in the channels, oldest first,
# then advance the normal cursor so cron picks up only new posts.
BACKFILL = os.environ.get("BACKFILL") == "1"
BACKFILL_LIMIT = int(os.environ.get("BACKFILL_LIMIT", "40"))  # per channel
BACKFILL_MIN_ID = int(os.environ.get("BACKFILL_MIN_ID", "0"))  # 0 = no floor


def fa_digits(value):
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def audio_duration(msg):
    """Media duration in seconds, or 0. `msg.audio` is a bare Document — the
    duration lives on its DocumentAttributeAudio, not on the Document itself."""
    document = getattr(msg, "audio", None) or getattr(msg, "voice", None)
    if document is None:
        return 0
    for attr in getattr(document, "attributes", None) or []:
        if isinstance(attr, DocumentAttributeAudio):
            return attr.duration or 0
    return 0


def long_media(msg):
    """audio = music/podcast file, voice = voice note; both carry duration."""
    document = getattr(msg, "audio", None) or getattr(msg, "voice", None)
    if document is not None and audio_duration(msg) >= MIN_SECONDS:
        return document
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


async def resolve_target(client, raw):
    """Turn TARGET_CHAT_ID into an entity we can post to.

    Chat -> -<id>. Supergroup/broadcast channel -> -100<id>. The raw id works
    only if the entity cache happens to be warm, so scan the dialogs once and
    match on the computed peer id.
    """
    raw = raw.strip()
    if not raw.lstrip("-").isdigit():
        return raw  # a @username: get_input_entity resolves that directly

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if str(utils.get_peer_id(entity)) == raw:
            print(f"target resolved: {entity.title!r} "
                  f"(id={entity.id}, broadcast={getattr(entity, 'broadcast', False)}, "
                  f"megagroup={getattr(entity, 'megagroup', False)})")
            return entity
    sys.exit(f"TARGET_CHAT_ID {raw} is not in this account's dialogs — "
             f"join the chat, or check the id")


def load_state():
    try:
        return json.loads((HERE / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return {"channels": {}}


def already_sent(state, key):
    """Dedup guard: has this exact file been forwarded before?

    Keyed on channel handle + message id, so a re-run, a retried send, or a
    manual replay cannot double-post the same file. The cursor alone cannot do
    this: a replay of an already-forwarded range would look brand new.
    """
    sent = state.setdefault("sent", {})
    return key in sent


def remember_sent(state, key):
    state.setdefault("sent", {})[key] = 1


def media_key(handle, msg):
    """Stable identity for one piece of media.

    handle + message id: the same song reposted into another channel has a
    different handle, and each channel is a separate source to watch.
    """
    return f"{handle}#{msg.id}"


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

    # A StringSession carries only the auth key, no entity cache. So a raw id
    # like -1001234567890 raises "Cannot find any entity" on a cold client even
    # when we belong to that chat. Walking the dialogs fills the cache and hands
    # back the real entity, which does resolve.
    target = await resolve_target(client, target)
    print(f"target: {target}")

    state = load_state()
    forwarded = failed = 0

    for handle, name in load_channels():
        try:
            entity = await client.get_entity(handle)
        except Exception as e:
            print(f"⚠️ {handle}: resolve failed ({type(e).__name__}), skipped")
            failed += 1
            continue

        if name.startswith("@"):
            # No display name in channels.txt — ask Telegram for the real title.
            name = getattr(entity, "title", None) or name

        if handle not in state["channels"] and not BACKFILL:
            # First sight of this channel: baseline only, older posts are not wanted.
            newest = await client.get_messages(entity, limit=1)
            if newest:
                state["channels"][handle] = newest[0].id
            print(f"⚪ {name}: baseline set, no history forwarded")
            continue

        if BACKFILL:
            # Walk backwards through everything older than the cursor and forward
            # every long file, oldest first. max_id pages towards older messages.
            try:
                cursor = state["channels"].get(handle, 0)
                sent = 0
                while sent < BACKFILL_LIMIT:
                    page = await client.get_messages(
                        entity, limit=100,
                        **({"max_id": cursor} if cursor else {}),
                        **({"min_id": BACKFILL_MIN_ID} if BACKFILL_MIN_ID else {}),
                    )
                    if not page:
                        break
                    # Telegram repeats the same page when max_id is at the very
                    # bottom of a channel; without this we would spin forever.
                    if page[-1].id >= cursor and cursor:
                        print(f"⬅️ {name}: reached the oldest message, stopping")
                        break
                    cursor = page[-1].id
                    # Oldest first so the backfill lands in chronological order.
                    for msg in reversed(page):
                        if long_media(msg) is None:
                            continue
                        key = media_key(handle, msg)
                        if already_sent(state, key):
                            # Already in the target channel: skip, but keep walking.
                            continue
                        await client.send_file(
                            target, msg.media,
                            caption=format_caption(name, audio_duration(msg)),
                        )
                        remember_sent(state, key)
                        sent += 1
                        clock = format_caption("", audio_duration(msg)).splitlines()[1]
                        print(f"⬅️ {name} — {clock} (id={msg.id})")
                        await asyncio.sleep(SEND_GAP)
                        if sent >= BACKFILL_LIMIT:
                            break
                print(f"⬅️ {name}: {sent} historical file(s) forwarded")
            except FloodWaitError as e:
                # One rate-limited channel must not kill the whole run.
                failed += 1
                print(f"⚠️ {handle}: flood wait {e.seconds}s, skipped")
            except Exception as e:
                failed += 1
                print(f"⚠️ {handle}: {type(e).__name__}: {e}")
            continue

        seen = state["channels"][handle]
        try:
            # min_id=seen excludes that id and everything older: exactly the new posts.
            # Paged because Telegram returns at most 100 per GetHistory call: with an
            # 8-hour cron a busy channel can post more than that, and a single call
            # would silently drop the overflow instead of sending it next run.
            scanned = 0
            while True:
                page = await client.get_messages(entity, limit=SCAN_PAGE, min_id=seen)
                if not page:
                    break
                scanned += len(page)
                # Oldest first, so the cursor never jumps past a post that then fails
                # to send — min() pinned it at the baseline and dropped anything past
                # the 100-message window.
                for msg in reversed(page):
                    media = long_media(msg)
                    if media is None:
                        # Nothing to send — safe to move the cursor past this post.
                        state["channels"][handle] = max(state["channels"][handle], msg.id)
                        continue
                    if already_sent(state, media_key(handle, msg)):
                        # Replay of an already-forwarded file: no second send, but the
                        # cursor still advances so this range is not re-read.
                        state["channels"][handle] = max(state["channels"][handle], msg.id)
                        continue
                    # Sending the media reference copies server-side: no download,
                    # no 50 MB cap, and the session account must be in the group.
                    await client.send_file(
                        target, msg.media,
                        caption=format_caption(name, audio_duration(msg)),
                    )
                    remember_sent(state, media_key(handle, msg))
                    # Cursor advances only after a confirmed send, so a failure
                    # retries this post on the next run instead of losing it.
                    state["channels"][handle] = max(state["channels"][handle], msg.id)
                    forwarded += 1
                    clock = format_caption("", audio_duration(msg)).splitlines()[1]
                    print(f"✅ {name} — {clock}")
                    await asyncio.sleep(SEND_GAP)

                # Nothing left above min_id=seen once the page stops advancing.
                if page[-1].id <= seen:
                    break
                seen = page[-1].id

            if scanned:
                print(f"   {name}: {scanned} new post(s)")
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

    # GitHub only greys a run green or red on the exit code. Returning 0 while
    # every channel failed hid a completely dead run behind a green check.
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())