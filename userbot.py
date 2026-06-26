"""
Per-user Telethon client management and mass campaign logic.

Performance changes vs original:
  - _CONCURRENCY raised from 15 → 50 (per-user concurrent sends)
  - asyncio.Queue worker pattern replaces semaphore+gather-all.
    Workers pull targets one at a time, so the event loop is never
    flooded with thousands of coroutines at once.
  - DB writes are batched every DB_BATCH sends (default 5) instead of
    after every single send, cutting DB overhead by 5×.
  - asyncio.sleep(0) yields between DB batches so other users' tasks
    (including /start handlers) can run without being starved.
"""
import asyncio
import logging
import os
import time
from telethon import TelegramClient
from telethon.tl.types import (
    User, InputUserEmpty,
    Channel, Chat,
    InputPeerChannel, InputPeerChat, InputPeerUser,
    PeerChannel, PeerChat,
)
from telethon.tl.functions.messages import (
    GetChatInviteImportersRequest,
    HideChatJoinRequestRequest,
    CheckChatInviteRequest,
)
from telethon.errors import FloodWaitError, SessionPasswordNeededError, UserPrivacyRestrictedError

from config import API_ID, API_HASH, SESSIONS_DIR
import database as db

logger = logging.getLogger(__name__)

_clients: dict[int, TelegramClient] = {}
_tasks: dict[int, asyncio.Task] = {}
_jr_tasks: dict[int, asyncio.Task] = {}

# ── Tuning knobs ──────────────────────────────────────────────────────────────
# Number of concurrent sends per user campaign.
# 50 saturates Telegram's per-account rate limit nicely; go higher at your own risk.
_CONCURRENCY = 50

# Write stats to DB every N successful sends.
# Lower = more accurate live counter; higher = faster campaign.
_DB_BATCH = 5


def session_path(user_id: int) -> str:
    return os.path.join(SESSIONS_DIR, f"user_{user_id}")


def get_client(user_id: int):
    return _clients.get(user_id)


async def create_client(user_id: int) -> TelegramClient:
    client = TelegramClient(
        session_path(user_id), API_ID, API_HASH,
        connection_retries=3,
        retry_delay=1,
        auto_reconnect=True,
    )
    await client.connect()
    _clients[user_id] = client
    return client


async def send_code(user_id: int, phone: str) -> str:
    client = await create_client(user_id)
    result = await client.send_code_request(phone)
    return result.phone_code_hash


async def sign_in(user_id: int, phone: str, code: str, phone_code_hash: str, password: str | None = None):
    client = _clients.get(user_id) or await create_client(user_id)
    try:
        user = await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        return user
    except SessionPasswordNeededError:
        if not password:
            raise
        user = await client.sign_in(password=password)
        return user


async def sign_in_2fa(user_id: int, password: str):
    client = _clients.get(user_id)
    if not client:
        raise RuntimeError("No client found")
    user = await client.sign_in(password=password)
    return user


async def load_existing_session(user_id: int) -> bool:
    """Load a Telethon session from disk if it exists. Non-blocking for other users."""
    path = session_path(user_id) + ".session"
    if not os.path.exists(path):
        return False
    if user_id in _clients:
        try:
            if await _clients[user_id].is_user_authorized():
                return True
        except Exception:
            pass
    try:
        client = await create_client(user_id)
        if await client.is_user_authorized():
            return True
        await client.disconnect()
        _clients.pop(user_id, None)
    except Exception:
        pass
    return False


async def disconnect_client(user_id: int):
    client = _clients.pop(user_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass


async def logout_user(user_id: int):
    client = _clients.get(user_id)
    if client:
        try:
            await client.log_out()
        except Exception:
            pass
    await disconnect_client(user_id)
    import glob
    for f in glob.glob(session_path(user_id) + "*"):
        try:
            os.remove(f)
        except Exception:
            pass


async def start_campaign(user_id: int, messages: list[dict], progress_cb, done_cb) -> bool:
    if user_id in _tasks and not _tasks[user_id].done():
        return False
    task = asyncio.create_task(
        _campaign_loop(user_id, messages, progress_cb, done_cb),
        name=f"campaign_{user_id}",
    )
    _tasks[user_id] = task
    return True


async def cancel_campaign(user_id: int):
    task = _tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _extract_invite_hash(ref: str) -> str | None:
    """Return the raw invite hash from a t.me/+ or t.me/joinchat/ URL, or None."""
    if "t.me/+" in ref:
        return ref.split("t.me/+")[-1].rstrip("/").split("?")[0]
    if "t.me/joinchat/" in ref:
        return ref.split("t.me/joinchat/")[-1].rstrip("/").split("?")[0]
    return None


async def _resolve_channel(client, channel_ref: str):
    """
    Resolve any channel reference to a Telethon entity.
    Used by the mass-DM campaign loop (dialogs-based).
    """
    ref = channel_ref.strip()
    invite_hash = _extract_invite_hash(ref)
    if invite_hash:
        result = await client(CheckChatInviteRequest(hash=invite_hash))
        return result.chat
    return await client.get_entity(ref)


async def _get_input_peer_for_requests(client, channel_ref: str):
    """
    Return a proper InputPeer for GetChatInviteImportersRequest /
    HideChatJoinRequestRequest.  Works for both admin and owner accounts on
    public AND private channels.

    The root problem with a naive approach: CheckChatInviteRequest returns a
    Channel/Chat object that is NOT yet in Telethon's session entity cache, so
    client.get_input_entity() on it fails with "Could not find the input entity".

    Fix strategy (tried in order):
      1. Build InputPeerChannel directly from the object's own access_hash
         (works when the invite-check result carries it — usual case).
      2. Search the account's dialogs for a matching channel_id and grab its
         access_hash from there (works when the account is already a member /
         admin and the channel is in their dialog list).
      3. Fall back to client.get_input_entity() for public channels whose
         entities are cached by get_entity().
    """
    ref = channel_ref.strip()
    invite_hash = _extract_invite_hash(ref)

    # ── Step 1: get the raw chat object ───────────────────────────────────
    if invite_hash:
        try:
            result = await client(CheckChatInviteRequest(hash=invite_hash))
            chat_obj = result.chat
        except Exception as ex:
            raise RuntimeError(f"Invalid invite link or account not in channel: {ex}")
    else:
        try:
            chat_obj = await client.get_entity(ref)
        except Exception as ex:
            raise RuntimeError(f"Could not find channel '{ref}': {ex}")

    # ── Step 2: build InputPeer from the object directly ──────────────────
    if isinstance(chat_obj, Channel):
        # Best case: the object already carries its own access_hash
        if getattr(chat_obj, "access_hash", None):
            return InputPeerChannel(
                channel_id=chat_obj.id,
                access_hash=chat_obj.access_hash,
            )
        # access_hash missing — search dialogs (account must be member/admin)
        async for dialog in client.iter_dialogs(limit=1000):
            ent = dialog.entity
            if isinstance(ent, Channel) and ent.id == chat_obj.id:
                if getattr(ent, "access_hash", None):
                    return InputPeerChannel(
                        channel_id=ent.id,
                        access_hash=ent.access_hash,
                    )
        raise RuntimeError(
            "Channel not found in your account's dialog list.\n"
            "Make sure your linked Telegram account is an admin or owner of that channel."
        )

    elif isinstance(chat_obj, Chat):
        return InputPeerChat(chat_id=chat_obj.id)

    else:
        # Fallback for anything else (already cached entities, etc.)
        return await client.get_input_entity(chat_obj)


async def get_pending_join_requests(user_id: int, channel: str) -> tuple[list, int]:
    client = _clients.get(user_id)
    if not client or not await client.is_user_authorized():
        raise RuntimeError("Not authorised. Please add your account first.")
    peer = await _get_input_peer_for_requests(client, channel)
    result = await client(GetChatInviteImportersRequest(
        peer=peer,
        offset_date=0,
        offset_user=InputUserEmpty(),
        limit=200,
        requested=True,
    ))
    return result.importers, result.count


async def accept_join_requests(user_id: int, channel: str, how_many: int) -> tuple[int, int]:
    client = _clients.get(user_id)
    if not client or not await client.is_user_authorized():
        raise RuntimeError("Not authorised. Please add your account first.")
    peer = await _get_input_peer_for_requests(client, channel)
    result = await client(GetChatInviteImportersRequest(
        peer=peer,
        offset_date=0,
        offset_user=InputUserEmpty(),
        limit=max(how_many, 200),
        requested=True,
    ))
    total = result.count
    to_accept = result.importers[:how_many]
    accepted = 0
    for importer in to_accept:
        input_user = None
        try:
            input_user = await client.get_input_entity(importer.user_id)
            await client(HideChatJoinRequestRequest(
                peer=peer,
                user_id=input_user,
                approved=True,
            ))
            accepted += 1
            await asyncio.sleep(0.05)
        except FloodWaitError as fw:
            await asyncio.sleep(fw.seconds + 2)
            try:
                await client(HideChatJoinRequestRequest(peer=peer, user_id=input_user, approved=True))
                accepted += 1
            except Exception:
                pass
        except Exception as ex:
            logger.warning(f"Accept join request failed for {importer.user_id}: {ex}")
    return accepted, total


_WATERMARK = "\n\n📩 This message was sent by @DmsForwardBot"


async def _send_message_item(client, entity, msg: dict):
    """Send one message item (text or media) to entity.

    Honours two extra flags stored on the message dict:
      link_preview_disabled — if True, sends with no link-preview card.

    A watermark (@DmsForwardBot) is automatically appended to every
    outgoing message (text content and media captions).
    """
    no_preview = bool(msg.get("link_preview_disabled"))

    if msg.get("media_path") and os.path.exists(msg["media_path"]):
        caption = (msg.get("content") or "") + _WATERMARK
        await client.send_file(
            entity,
            msg["media_path"],
            caption=caption,
        )
    elif msg.get("content"):
        await client.send_message(
            entity,
            msg["content"] + _WATERMARK,
            link_preview=not no_preview,
        )


async def _campaign_loop(user_id: int, messages: list[dict], progress_cb, done_cb):
    """
    Queue-based concurrent campaign loop.

    Architecture:
      1. All target users are put into an asyncio.Queue.
      2. _CONCURRENCY worker coroutines are spawned — each pulls one
         target at a time, sends the messages, then pulls the next.
      3. DB writes are batched every _DB_BATCH sends.
      4. After each DB batch we call asyncio.sleep(0) to yield back to
         the event loop so other users' /start commands and button
         presses are not starved.
    """
    client = _clients.get(user_id)
    if not client or not await client.is_user_authorized():
        await done_cb(user_id, "Not authorised. Please add your account first.")
        return

    is_premium = await db.check_premium_active(user_id)
    stats = await db.get_stats(user_id) or {}
    already_sent = stats.get("total_sent", 0)
    free_limit = await db.get_free_limit()
    limit = 999_999 if is_premium else max(0, free_limit - already_sent)

    try:
        dialogs = await client.get_dialogs(limit=None)
        me = await client.get_me()
        targets = [
            d.entity for d in dialogs
            if isinstance(d.entity, User)
            and not d.entity.bot
            and d.entity.id != me.id
        ]

        total = len(targets)
        await db.create_campaign(user_id)
        await db.update_campaign(user_id, total=total, sent=0, status="running")
        await progress_cb(user_id, 0, total, None)

        queue: asyncio.Queue = asyncio.Queue()
        for t in targets:
            queue.put_nowait(t)

        sent_count = 0
        db_pending = 0
        stopped = False
        last_db_flush = time.monotonic()

        async def flush_db():
            nonlocal db_pending, last_db_flush
            if db_pending > 0:
                await db.increment_sent(user_id, db_pending)
                await db.update_campaign(user_id, sent=sent_count, status="running")
                db_pending = 0
                last_db_flush = time.monotonic()
                await asyncio.sleep(0)

        async def worker():
            nonlocal sent_count, db_pending, stopped
            while True:
                try:
                    entity = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if stopped:
                    queue.task_done()
                    break

                if sent_count >= limit:
                    stopped = True
                    queue.task_done()
                    break
                sent_count += 1

                send_ok = False
                try:
                    for msg in messages:
                        await _send_message_item(client, entity, msg)
                    send_ok = True

                    db_pending += 1
                    label = (
                        getattr(entity, "username", None)
                        or getattr(entity, "first_name", None)
                        or str(entity.id)
                    )
                    await progress_cb(user_id, sent_count, total, label)

                    if db_pending >= _DB_BATCH:
                        await flush_db()

                except FloodWaitError as fw:
                    logger.warning(f"FloodWait {fw.seconds}s for user {user_id}")
                    await flush_db()
                    await asyncio.sleep(fw.seconds + 1)
                    try:
                        for msg in messages:
                            await _send_message_item(client, entity, msg)
                        send_ok = True
                        db_pending += 1
                    except Exception:
                        pass

                except UserPrivacyRestrictedError:
                    pass

                except asyncio.CancelledError:
                    sent_count -= 1
                    queue.task_done()
                    raise

                except Exception as ex:
                    logger.debug(f"Skip {entity.id}: {ex}")

                finally:
                    if not send_ok:
                        sent_count -= 1
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(_CONCURRENCY)]
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            for w in workers:
                w.cancel()
            raise

        await flush_db()

        if not is_premium and sent_count >= limit:
            await done_cb(user_id, "free_limit")
        else:
            await db.update_campaign(user_id, status="done")
            await done_cb(user_id, None)

    except asyncio.CancelledError:
        await db.update_campaign(user_id, status="cancelled")
        raise
    except Exception as ex:
        logger.error(f"Campaign error for {user_id}: {ex}")
        await db.update_campaign(user_id, status="error")
        await done_cb(user_id, str(ex))


# ── Join Request DM Campaign ──────────────────────────────────────────────────

async def start_jr_campaign(
    user_id: int,
    channel: str,
    how_many: int,
    messages: list[dict],
    progress_cb,
    done_cb,
) -> bool:
    """Start a Join Request DM campaign. Sends DMs to pending join requesters
    WITHOUT accepting or dismissing their join request."""
    if user_id in _jr_tasks and not _jr_tasks[user_id].done():
        return False
    task = asyncio.create_task(
        _jr_campaign_loop(user_id, channel, how_many, messages, progress_cb, done_cb),
        name=f"jr_campaign_{user_id}",
    )
    _jr_tasks[user_id] = task
    return True


async def cancel_jr_campaign(user_id: int):
    task = _jr_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _jr_campaign_loop(
    user_id: int,
    channel: str,
    how_many: int,
    messages: list[dict],
    progress_cb,
    done_cb,
):
    """
    Sends a DM to each pending join requester of `channel` up to `how_many`.
    Does NOT accept or dismiss their join request — purely sends the message.
    """
    client = _clients.get(user_id)
    if not client or not await client.is_user_authorized():
        await done_cb(user_id, "Not authorised. Please add your account first.")
        return

    try:
        # Use the robust peer resolver — works for both admin and owner
        peer = await _get_input_peer_for_requests(client, channel)

        # Fetch up to how_many requesters, paginating 200 at a time
        fetched = []
        offset_date = 0
        offset_user = InputUserEmpty()

        while len(fetched) < how_many:
            batch_limit = min(200, how_many - len(fetched))
            result = await client(GetChatInviteImportersRequest(
                peer=peer,
                offset_date=offset_date,
                offset_user=offset_user,
                limit=batch_limit,
                requested=True,
            ))
            if not result.importers:
                break
            fetched.extend(result.importers)
            if len(result.importers) < batch_limit:
                break
            # Prepare next page offset
            last = result.importers[-1]
            offset_date = int(last.date.timestamp()) if hasattr(last.date, "timestamp") else int(last.date)
            try:
                offset_user = await client.get_input_entity(last.user_id)
            except Exception:
                offset_user = InputUserEmpty()

        importers = fetched[:how_many]
        total = len(importers)

        if total == 0:
            await done_cb(user_id, "no_requests")
            return

        await progress_cb(user_id, 0, total, None)

        sent_count = 0

        for importer in importers:
            # Check for cancellation
            if asyncio.current_task().cancelled():
                raise asyncio.CancelledError()

            try:
                user_entity = await client.get_entity(importer.user_id)
                for msg in messages:
                    await _send_message_item(client, user_entity, msg)
                sent_count += 1
                label = (
                    getattr(user_entity, "username", None)
                    or getattr(user_entity, "first_name", None)
                    or str(importer.user_id)
                )
                await progress_cb(user_id, sent_count, total, label)
                # Small delay to avoid Telegram flood limits
                await asyncio.sleep(0.4)

            except FloodWaitError as fw:
                logger.warning(f"JR FloodWait {fw.seconds}s for user {user_id}")
                await asyncio.sleep(fw.seconds + 2)
                try:
                    user_entity = await client.get_entity(importer.user_id)
                    for msg in messages:
                        await _send_message_item(client, user_entity, msg)
                    sent_count += 1
                    await progress_cb(user_id, sent_count, total, str(importer.user_id))
                    await asyncio.sleep(0.4)
                except Exception:
                    pass

            except UserPrivacyRestrictedError:
                # User has privacy settings preventing DMs — skip silently
                pass

            except asyncio.CancelledError:
                raise

            except Exception as ex:
                logger.debug(f"JR DM skip {importer.user_id}: {ex}")

        await done_cb(user_id, None)

    except asyncio.CancelledError:
        raise
    except Exception as ex:
        logger.error(f"JR campaign error for {user_id}: {ex}")
        await done_cb(user_id, str(ex))
