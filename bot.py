"""
User-facing bot — fully advanced UI & features.
"""
import asyncio
import hashlib
import logging
import os
import secrets
import time
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore", category=UserWarning)

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    MessageHandler, ContextTypes, ConversationHandler, filters,
)

import database as db
import userbot
import gmail_checker
from assets_data import get_image_bytes
from config import (
    BOT_TOKEN, UPI_ID, PLANS, BOT_USERNAME,
    ADMIN_USERNAME, FREE_DM_LIMIT, ADMIN_TG_ID, FREE_ACCEPT_LIMIT
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_progress_msg_ids: dict[int, int] = {}
_progress_last_edit: dict[int, float] = {}
_campaign_start_times: dict[int, float] = {}

# Join Request DM campaign tracking
_jr_progress_msg_ids: dict[int, int] = {}
_jr_progress_last_edit: dict[int, float] = {}
_jr_campaign_start_times: dict[int, float] = {}

# ── Order timer state ──────────────────────────────────────────────────────────
# Tracks active payment countdown timers per user
_order_timers: dict[int, asyncio.Task] = {}
_order_info: dict[int, dict] = {}
_ORDER_TIMEOUT = 300  # 5 minutes in seconds

# ── Plans cache (loaded from DB at startup, reloaded by admin bot after edits) ─
_PLANS_CACHE: dict = {k: dict(v) for k, v in PLANS.items()}


async def reload_plans():
    global _PLANS_CACHE
    _PLANS_CACHE = await db.get_plans()
    logger.info("Plans reloaded: %s", list(_PLANS_CACHE.keys()))

DIVIDER = "─" * 22


def _build_progress_bar(sent: int, total: int, bar_len: int = 18) -> str:
    if total == 0:
        return "░" * bar_len
    filled = round(bar_len * sent / total)
    return "█" * filled + "░" * (bar_len - filled)


def _live_counter_ui(sent: int, total: int, speed: float, last: str, channel: str = "") -> str:
    """
    Render a clean, live-countable progress block for campaigns.
    No loading spinner — just real numbers updating in real time.
    """
    pct = round(100 * sent / total) if total else 0
    bar = _build_progress_bar(sent, total, bar_len=20)
    remaining = max(0, total - sent)
    eta_str = ""
    if speed > 0 and remaining > 0:
        eta_sec = remaining / speed
        if eta_sec < 60:
            eta_str = f"⏱ ETA: *{int(eta_sec)}s*\n"
        else:
            eta_str = f"⏱ ETA: *{int(eta_sec // 60)}m {int(eta_sec % 60)}s*\n"
    channel_line = f"📣 Channel: `{channel}`\n" if channel else ""
    return (
        f"{channel_line}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅  Sent:       *{sent:,}*\n"
        f"📭  Remaining:  *{remaining:,}*\n"
        f"📊  Total:      *{total:,}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{pct}%  {bar}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡  Speed: *{speed:.1f}* msg/s\n"
        f"{eta_str}"
        f"📍  Last: {last or '—'}"
    )


def _stop_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛔ Stop Campaign", callback_data="cb_stop_campaign")],
    ])


def _jr_stop_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛔ Stop JR Campaign", callback_data="cb_jr_stop")],
    ])


# ── Conversation states ───────────────────────────────────────────────────────
(
    ADD_PHONE, ADD_CODE, ADD_2FA,
    SET_MSG_COLLECT,
    PAY_UTR,
    GIFT_CODE_INPUT,
    PAY_UTR_AUTO,
    AP_CHANNEL_INPUT,
    AP_COUNT_INPUT,
    JR_CHANNEL_INPUT,
    JR_COUNT_INPUT,
    JR_MSG_COLLECT,
) = range(12)


# ── Keyboards ─────────────────────────────────────────────────────────────────
# Cache of custom buttons added by admin — refreshed on startup and after admin changes
_EXTRA_BUTTONS: list = []


async def reload_custom_buttons():
    """Fetch custom buttons from DB and update the in-memory cache."""
    global _EXTRA_BUTTONS
    _EXTRA_BUTTONS = await db.get_custom_buttons()


def main_menu_kb():
    rows = [
        [InlineKeyboardButton("🚀 Start Mass DM Campaign", callback_data="cb_campaign")],
        [InlineKeyboardButton("✉️ Set Message", callback_data="cb_setmsg"),
         InlineKeyboardButton("📋 Preview Message", callback_data="cb_previewmsg")],
        [InlineKeyboardButton("📊 My Stats", callback_data="cb_stats"),
         InlineKeyboardButton("👤 My Account", callback_data="cb_myaccount")],
        [InlineKeyboardButton("👑 Go VIP Premium", callback_data="cb_premium"),
         InlineKeyboardButton("🎁 Redeem Code", callback_data="cb_giftcode")],
        [InlineKeyboardButton("➕ Add Account", callback_data="cb_addaccount"),
         InlineKeyboardButton("➖ Remove Account", callback_data="cb_removeaccount")],
        [InlineKeyboardButton("✅ Accept Pending", callback_data="cb_acceptpending"),
         InlineKeyboardButton("📨 Join Request DM", callback_data="cb_jr_dm")],
        [InlineKeyboardButton("🔗 Refer & Earn", callback_data="cb_refer")],
        [InlineKeyboardButton("📖 How to Use", callback_data="cb_tutorial"),
         InlineKeyboardButton("💬 Support", url="https://t.me/shubhxseller")],
    ]
    for btn in _EXTRA_BUTTONS:
        rows.append([InlineKeyboardButton(btn["label"], url=btn["url"])])
    return InlineKeyboardMarkup(rows)


def premium_plans_kb():
    _ICONS = ["⚡", "🔥", "💎", "🏆", "👑", "🌟", "🔮", "✨", "💫", "⭐"]
    rows = []
    sorted_plans = sorted(_PLANS_CACHE.items(), key=lambda x: x[1].get("days", 0))
    for i, (key, plan) in enumerate(sorted_plans):
        icon = _ICONS[i % len(_ICONS)]
        rows.append([InlineKeyboardButton(
            f"{icon} {plan['label']} — ₹{plan['price']}",
            callback_data=f"plan_{key}",
        )])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="cb_back")])
    return InlineKeyboardMarkup(rows)


def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="cb_back")]])


def done_kb(count: int = 0):
    label = f"✅ Done — {count} message(s) saved" if count else "✅ Done — Save & Finish"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="msg_done")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="cb_back")],
    ])


def jr_done_kb(count: int = 0):
    label = f"✅ Done — {count} message(s) saved" if count else "✅ Done — Save & Finish"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="jr_msg_done")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="cb_back")],
    ])


def jr_ready_kb():
    """Shown after user finishes composing the JR DM message."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start Campaign", callback_data="cb_jr_start")],
        [InlineKeyboardButton("📋 Preview Message", callback_data="cb_jr_preview")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cb_jr_cancel")],
    ])


def paid_kb(plan_key):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I've Paid — Submit UTR", callback_data=f"ipaid_{plan_key}")],
        [InlineKeyboardButton("🔙 Choose Different Plan", callback_data="cb_premium")],
    ])


def payment_method_kb(plan_key):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Admin Approval", callback_data=f"paymethod_admin_{plan_key}")],
        [InlineKeyboardButton("⚡ Automatic Pay", callback_data=f"paymethod_auto_{plan_key}")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="cb_premium")],
    ])


def paid_auto_kb(plan_key):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I've Paid — Submit UTR", callback_data=f"ipaid_auto_{plan_key}")],
        [InlineKeyboardButton("🔙 Choose Different Plan", callback_data="cb_premium")],
    ])


# ── Order receipt helpers ──────────────────────────────────────────────────────

def _order_action_kb(plan_key: str, mode: str) -> InlineKeyboardMarkup:
    """Keyboard shown on the ORDER CREATED receipt (timer still running)."""
    ipaid_cb = f"ipaid_{plan_key}" if mode == "admin" else f"ipaid_auto_{plan_key}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I've Paid — Submit UTR", callback_data=ipaid_cb)],
        [InlineKeyboardButton("🔙 Choose Different Plan", callback_data="cb_premium")],
    ])


def _order_expired_kb(plan_key: str, mode: str) -> InlineKeyboardMarkup:
    """Keyboard shown after the 5-min timer expires."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Recreate Order", callback_data=f"order_retry_{plan_key}_{mode}")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="cb_back")],
    ])


def _timer_bar(remaining: float, total: float = _ORDER_TIMEOUT) -> str:
    """Return a 10-block ASCII progress bar for remaining time."""
    pct = max(0.0, min(1.0, remaining / total))
    filled = round(pct * 10)
    return "█" * filled + "░" * (10 - filled)


def _format_order_msg(info: dict, remaining: float) -> str:
    mins = int(remaining // 60)
    secs = int(remaining % 60)
    bar = _timer_bar(remaining)
    method_label = "Admin Approval" if info["mode"] == "admin" else "⚡ Auto (FamPay)"
    return (
        f"📋 *ORDER CREATED*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔖 Order ID: `{info['order_id']}`\n"
        f"📦 Plan: *{info['plan_label']}*\n"
        f"⏳ Duration: *{info['days']} day(s)*\n"
        f"💰 Amount: *₹{info['amount']}*\n"
        f"💳 Method: {method_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏦 UPI ID: `{info['upi']}`\n\n"
        f"⏰ Pay within: *{mins}:{secs:02d}*\n"
        f"{bar}\n\n"
        f"Scan the QR or copy the UPI ID above.\n"
        f"Tap ✅ *I've Paid* once done.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )


def _format_expired_msg(info: dict) -> str:
    return (
        f"❌ *PAYMENT EXPIRED*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔖 Order ID: `{info['order_id']}`\n"
        f"📦 Plan: *{info['plan_label']}*\n"
        f"💰 Amount: *₹{info['amount']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ *0:00* — Time's up!\n\n"
        f"Tap 🔄 *Recreate Order* to start again.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━"
    )


async def _edit_order_message(bot, info: dict, text: str, kb: InlineKeyboardMarkup):
    """Edit the order message regardless of whether it's a photo or plain text."""
    chat_id = info["chat_id"]
    message_id = info["message_id"]
    try:
        if info.get("has_photo"):
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id,
                caption=text, parse_mode="Markdown", reply_markup=kb,
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=text, parse_mode="Markdown", reply_markup=kb,
            )
    except Exception:
        pass


async def _order_timer_loop(bot, user_id: int):
    """Background task: updates countdown every 30 s; marks expired at 0:00."""
    info = _order_info.get(user_id)
    if not info:
        return
    deadline = info["deadline"]

    try:
        # Update every 30 seconds
        while True:
            await asyncio.sleep(30)
            info = _order_info.get(user_id)
            if not info:
                return  # order was cancelled / UTR submitted

            remaining = deadline - time.time()

            if remaining <= 0:
                # Expired — show expired state
                expired_text = _format_expired_msg(info)
                expired_kb = _order_expired_kb(info["plan_key"], info["mode"])
                await _edit_order_message(bot, info, expired_text, expired_kb)
                _order_info.pop(user_id, None)
                return

            # Still running — update the clock
            updated_text = _format_order_msg(info, remaining)
            active_kb = _order_action_kb(info["plan_key"], info["mode"])
            await _edit_order_message(bot, info, updated_text, active_kb)

    except asyncio.CancelledError:
        pass


def _start_order_timer(bot, user_id: int):
    """Cancel any previous timer for this user and start a fresh one."""
    old = _order_timers.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(
        _order_timer_loop(bot, user_id),
        name=f"order_timer_{user_id}",
    )
    _order_timers[user_id] = task


async def _create_order_and_send(update, ctx, plan_key: str, mode: str):
    """
    Shared logic for admin and auto payment methods:
      1. Create DB payment record (no UTR yet).
      2. Show ORDER CREATED receipt with QR photo.
      3. Start 5-min countdown timer.
    """
    plan = _PLANS_CACHE.get(plan_key)
    if not plan:
        await update.callback_query.message.reply_text("⚠️ Plan not found.", reply_markup=back_kb())
        return

    user_id = update.effective_user.id
    upi = await db.get_setting("upi_id", UPI_ID)

    # Create the payment record in DB now (UTR comes later)
    payment = await db.create_payment(user_id, plan_key, plan["price"])
    order_id = payment["order_id"]

    # Store order info for the timer task
    deadline = time.time() + _ORDER_TIMEOUT
    info = {
        "order_id": order_id,
        "payment_id": payment["id"],
        "plan_key": plan_key,
        "plan_label": plan["label"],
        "days": plan["days"],
        "amount": plan["price"],
        "upi": upi,
        "mode": mode,
        "deadline": deadline,
        "chat_id": update.effective_chat.id,
        "message_id": None,  # filled in after send
        "has_photo": False,
    }

    # Store order_id in user session so UTR handler can link to it
    ctx.user_data["active_order_id"] = order_id
    ctx.user_data["active_payment_id"] = payment["id"]

    receipt_text = _format_order_msg(info, _ORDER_TIMEOUT)
    kb = _order_action_kb(plan_key, mode)

    # Try to send with QR photo
    sent = None
    try:
        sent = await update.callback_query.message.reply_photo(
            photo=get_image_bytes("qr"),
            caption=receipt_text,
            parse_mode="Markdown",
            reply_markup=kb,
        )
        info["has_photo"] = True
    except Exception:
        sent = await update.callback_query.message.reply_text(
            receipt_text, parse_mode="Markdown", reply_markup=kb,
        )
        info["has_photo"] = False

    info["message_id"] = sent.message_id
    _order_info[user_id] = info
    _start_order_timer(ctx.bot, user_id)


# ── Helpers ───────────────────────────────────────────────────────────────────
async def _premium_badge(user_id: int) -> str:
    is_active = await db.check_premium_active(user_id)
    if not is_active:
        return "🆓 Free Plan"
    prem = await db.get_premium(user_id)
    if prem:
        try:
            exp = datetime.fromisoformat(prem["expires_at"])
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            diff = (exp - now).days
            if diff <= 0:
                return "⚠️ Premium Expired"
            return f"👑 VIP Premium — {diff}d left"
        except Exception:
            return "👑 VIP Premium"
    return "👑 VIP Premium"


async def _support_url() -> str:
    handle = await db.get_setting("support_username", ADMIN_USERNAME)
    return f"https://t.me/{handle}"


# ── Guards ────────────────────────────────────────────────────────────────────
async def ensure_account(update: Update) -> bool:
    user_id = update.effective_user.id
    acc = await db.get_account(user_id)
    if acc:
        return True
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Account Now", callback_data="cb_addaccount")],
        [InlineKeyboardButton("📖 How to Use", callback_data="cb_tutorial")],
    ])
    user = update.effective_user
    name = user.first_name or "there"
    msg = (
        f"🔐 *Account Not Linked, {name}!*\n\n"
        f"{DIVIDER}\n"
        f"⚡ This feature requires your Telegram account to be linked.\n\n"
        f"*How to link in 3 steps:*\n"
        f"1️⃣ Tap *Add Account Now* below\n"
        f"2️⃣ Enter your phone number with country code\n"
        f"   _(e.g. +91XXXXXXXXXX)_\n"
        f"3️⃣ Enter the OTP Telegram sends you\n\n"
        f"✅ Done! Takes less than 30 seconds.\n"
        f"{DIVIDER}\n"
        f"_Linking is safe and only used for sending DMs._"
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    return False


async def ensure_not_banned(update: Update) -> bool:
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    if user and user.get("is_banned"):
        await update.effective_message.reply_text(
            "🚫 *You have been banned.*\n\n"
            "Contact support if you believe this is a mistake.",
            parse_mode="Markdown",
        )
        return False
    return True


# ── Force join ────────────────────────────────────────────────────────────────
def _is_private_link(ch: str) -> bool:
    """True when `ch` is a private Telegram invite link, not a public username."""
    return ch.startswith("https://") or "joinchat" in ch or "t.me/+" in ch


async def _check_force_join(bot, user_id: int) -> list[dict]:
    channels = await db.get_force_join_channels()
    if not channels:
        return []

    confirmed_privates = await db.get_user_private_joins(user_id)

    pending = []
    for ch in channels:
        if _is_private_link(ch):
            if ch not in confirmed_privates:
                pending.append({
                    "type": "private",
                    "id": ch,
                    "url": ch,
                    "label": "🔒 Join Private Channel",
                })
        else:
            try:
                member = await bot.get_chat_member(f"@{ch}", user_id)
                if member.status in ("left", "kicked", "banned"):
                    pending.append({
                        "type": "public",
                        "id": ch,
                        "url": f"https://t.me/{ch}",
                        "label": f"📣 Join @{ch}",
                    })
            except Exception:
                pending.append({
                    "type": "public",
                    "id": ch,
                    "url": f"https://t.me/{ch}",
                    "label": f"📣 Join @{ch}",
                })
    return pending


# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.upsert_user(user.id, username=user.username or "")

    if ctx.args:
        arg = ctx.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg[4:])
                if referrer_id != user.id:
                    await db.set_referral(user.id, referrer_id)
            except (ValueError, TypeError):
                pass

    if not await ensure_not_banned(update):
        return

    pending_channels = await _check_force_join(ctx.bot, user.id)
    if pending_channels:
        rows = [[InlineKeyboardButton(ch["label"], url=ch["url"])] for ch in pending_channels]
        rows.append([InlineKeyboardButton("✅ I've Joined All", callback_data="cb_check_joined")])
        await update.message.reply_text(
            "📣 *Channel Membership Required*\n\n"
            f"{DIVIDER}\n"
            "You must join all channels below to use this bot.\n"
            "After joining *every* channel, tap *I've Joined All*:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    await userbot.load_existing_session(user.id)
    acc = await db.get_account(user.id)
    stats = await db.get_stats(user.id) or {}
    badge = await _premium_badge(user.id)
    custom_welcome = await db.get_setting("welcome_text", "")
    uname = f"@{user.username}" if user.username else "N/A"

    if acc:
        total_sent = stats.get("total_sent", 0)
        text = (
            f"👋 *Welcome back, {user.first_name}!*\n\n"
            f"{DIVIDER}\n"
            f"🆔 User ID: `{user.id}`\n"
            f"👤 Username: {uname}\n"
            f"📱 Account: `{acc['phone']}`\n"
            f"💎 Status: {badge}\n"
            f"📨 Total Sent: *{total_sent:,}* DMs\n"
            f"{DIVIDER}\n\n"
            "Choose an option below 👇"
        )
    elif custom_welcome:
        text = custom_welcome
    else:
        text = (
            f"🤖 *AUTO DMs BOT*\n\n"
            f"{DIVIDER}\n"
            f"🆔 Your ID: `{user.id}`\n"
            f"👤 Username: {uname}\n"
            f"{DIVIDER}\n\n"
            "🚀 *The fastest mass DM tool on Telegram*\n\n"
            "✅ Send to *ALL your DMs* at once\n"
            "⚡ Blazing-fast delivery\n"
            "🆓 Free plan: 100 sends\n"
            "👑 Premium: Unlimited sends\n\n"
            f"{DIVIDER}\n"
            "👇 Tap *Add Account* to get started!"
        )

    try:
        await update.message.reply_photo(
            photo=get_image_bytes("welcome"), caption=text,
            parse_mode="Markdown", reply_markup=main_menu_kb(),
        )
    except Exception:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())


async def cb_check_joined(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Checking membership…")
    user = update.effective_user

    all_channels = await db.get_force_join_channels()
    confirmed_privates = await db.get_user_private_joins(user.id)
    for ch in all_channels:
        if _is_private_link(ch) and ch not in confirmed_privates:
            await db.mark_private_joined(user.id, ch)

    pending_channels = await _check_force_join(ctx.bot, user.id)
    if pending_channels:
        rows = [[InlineKeyboardButton(ch["label"], url=ch["url"])] for ch in pending_channels]
        rows.append([InlineKeyboardButton("✅ I've Joined All", callback_data="cb_check_joined")])
        await q.message.edit_text(
            "❌ *Still not all joined!*\n\n"
            "You haven't joined all required channels yet.\n"
            "Please join the ones below, then tap *I've Joined All* again:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    await userbot.load_existing_session(user.id)
    acc = await db.get_account(user.id)
    stats = await db.get_stats(user.id) or {}
    badge = await _premium_badge(user.id)
    custom_welcome = await db.get_setting("welcome_text", "")
    uname = f"@{user.username}" if user.username else "N/A"

    if acc:
        text = (
            f"👋 *Welcome back, {user.first_name}!*\n\n"
            f"{DIVIDER}\n"
            f"🆔 User ID: `{user.id}`\n"
            f"👤 Username: {uname}\n"
            f"📱 Account: `{acc['phone']}`\n"
            f"💎 Status: {badge}\n"
            f"📨 Total Sent: *{stats.get('total_sent', 0):,}* DMs\n"
            f"{DIVIDER}\n\nChoose an option below 👇"
        )
    elif custom_welcome:
        text = custom_welcome
    else:
        text = (
            f"✅ *All channels joined! Welcome, {user.first_name}!*\n\n"
            f"{DIVIDER}\n"
            f"🆔 Your ID: `{user.id}`\n"
            f"👤 Username: {uname}\n"
            f"{DIVIDER}\n\n"
            "👇 Tap *Add Account* to get started!"
        )

    await q.message.delete()
    try:
        await ctx.bot.send_photo(
            chat_id=user.id, photo=get_image_bytes("welcome"), caption=text,
            parse_mode="Markdown", reply_markup=main_menu_kb(),
        )
    except Exception:
        await ctx.bot.send_message(
            chat_id=user.id, text=text,
            parse_mode="Markdown", reply_markup=main_menu_kb()
        )


# ── Back ──────────────────────────────────────────────────────────────────────
async def cb_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = update.effective_user
    acc = await db.get_account(user.id)
    badge = await _premium_badge(user.id)
    uname = f"@{user.username}" if user.username else "N/A"

    if acc:
        text = (
            f"🏠 *Main Menu*\n\n"
            f"{DIVIDER}\n"
            f"🆔 {user.id}  |  👤 {uname}\n"
            f"💎 {badge}\n"
            f"{DIVIDER}\n\nChoose an option 👇"
        )
    else:
        text = (
            f"🏠 *Main Menu*\n\n"
            f"{DIVIDER}\n"
            f"🆔 Your ID: `{user.id}`\n"
            f"{DIVIDER}\n\nChoose an option 👇"
        )
    try:
        await q.message.edit_caption(caption=text, parse_mode="Markdown", reply_markup=main_menu_kb())
    except Exception:
        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())


# ── My Account ────────────────────────────────────────────────────────────────
async def cb_myaccount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await ensure_account(update):
        return
    if not await ensure_not_banned(update):
        return
    user = update.effective_user
    user_id = user.id

    user_row = await db.get_user(user_id)
    acc = await db.get_account(user_id)
    prem = await db.get_premium(user_id)
    stats = await db.get_stats(user_id) or {}
    is_active = await db.check_premium_active(user_id)
    uname = f"@{user.username}" if user.username else "N/A"
    joined = str(user_row.get("created_at", "N/A"))[:10] if user_row else "N/A"

    if is_active and prem:
        try:
            exp = datetime.fromisoformat(prem["expires_at"])
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            diff = max(0, (exp - now).days)
            plan_status = f"👑 *VIP Premium*\n   Plan: `{prem['plan_key']}`\n   Expires: `{prem['expires_at'][:10]}`\n   ⏳ {diff} day(s) remaining"
        except Exception:
            plan_status = "👑 *VIP Premium* (active)"
    else:
        used = stats.get("total_sent", 0)
        fl = await db.get_free_limit()
        remaining = max(0, fl - used)
        plan_status = f"🆓 *Free Plan*\n   Sends used: `{used}` / `{fl}`\n   Remaining: `{remaining}`"

    msg = (
        f"👤 *MY PROFILE*\n"
        f"{DIVIDER}\n"
        f"🆔 User ID: `{user_id}`\n"
        f"👤 Username: {uname}\n"
        f"📱 Phone: `{acc['phone'] if acc else 'Not linked'}`\n"
        f"📅 Joined: `{joined}`\n\n"
        f"💎 *PLAN STATUS*\n"
        f"{DIVIDER}\n"
        f"{plan_status}\n\n"
        f"📊 *STATISTICS*\n"
        f"{DIVIDER}\n"
        f"📨 Total DMs Sent: *{stats.get('total_sent', 0):,}*\n"
        f"💰 Plans Purchased: *{stats.get('plans_bought', 0)}*\n"
    )
    await q.message.reply_text(msg, parse_mode="Markdown", reply_markup=back_kb())


# ── My Stats ──────────────────────────────────────────────────────────────────
async def cb_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await ensure_account(update):
        return
    if not await ensure_not_banned(update):
        return
    user_id = update.effective_user.id

    stats = await db.get_stats(user_id) or {}
    camp = await db.get_campaign(user_id)
    is_active = await db.check_premium_active(user_id)
    prem = await db.get_premium(user_id)

    total_sent = stats.get("total_sent", 0)
    plans = stats.get("plans_bought", 0)

    last_camp = "None yet"
    if camp:
        status_map = {"done": "✅ Completed", "running": "🔄 Running", "cancelled": "⛔ Stopped", "error": "❌ Error"}
        last_camp = f"{status_map.get(camp['status'], camp['status'])} — {camp['sent']}/{camp['total']} sent"

    if is_active and prem:
        try:
            exp = datetime.fromisoformat(prem["expires_at"])
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            diff = max(0, (exp - now).days)
            plan_line = f"👑 VIP Premium — {diff}d left"
        except Exception:
            plan_line = "👑 VIP Premium"
    else:
        used = total_sent
        _fl = await db.get_free_limit()
        plan_line = f"🆓 Free — {max(0, _fl - used)} sends left"

    msg = (
        f"📊 *YOUR STATISTICS*\n"
        f"{DIVIDER}\n"
        f"💎 Plan: {plan_line}\n\n"
        f"📨 *Sending*\n"
        f"   Total DMs Sent: *{total_sent:,}*\n"
        f"   Plans Purchased: *{plans}*\n\n"
        f"🎯 *Last Campaign*\n"
        f"   {last_camp}\n"
        f"{DIVIDER}\n"
        f"_Keep sending to grow your reach!_ 🚀"
    )
    await q.message.reply_text(msg, parse_mode="Markdown", reply_markup=back_kb())


# ── Tutorial ──────────────────────────────────────────────────────────────────
async def cb_tutorial(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    support = await db.get_setting("support_username", ADMIN_USERNAME)
    text = (
        f"📖 *HOW TO USE AUTO DMs BOT*\n"
        f"{DIVIDER}\n\n"
        "*Step 1 — Add Account*\n"
        "Tap *Add Account* and enter your phone number with country code (e.g. +91XXXXXXXXXX), then enter the OTP.\n\n"
        "*Step 2 — Set Message*\n"
        "Tap *Set Message* and send the text, link, or image you want to DM.\n\n"
        "*Step 3 — Start Campaign*\n"
        "Tap *Start Mass DM Campaign* — the bot will send your message to all your contacts.\n\n"
        "*Join Request DM*\n"
        "Tap *Join Request DM* to send a message to people who have pending join requests in your channel — without accepting or dismissing them.\n\n"
        f"{DIVIDER}\n"
        f"💬 Support: @{support}"
    )
    await q.message.reply_text(text, parse_mode="Markdown", reply_markup=back_kb())


# ── Preview Message ───────────────────────────────────────────────────────────
async def cb_previewmsg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    msgs = await db.get_user_messages(user_id)
    if not msgs:
        await q.message.reply_text(
            "📭 *No Message Set*\n\nTap *Set Message* first.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Set Message", callback_data="cb_setmsg")],
                [InlineKeyboardButton("🔙 Back", callback_data="cb_back")],
            ]),
        )
        return

    await q.message.reply_text(
        f"📋 *YOUR CAMPAIGN MESSAGE(S)*\n{DIVIDER}\n_{len(msgs)} message(s) set:_\n",
        parse_mode="Markdown",
        reply_markup=back_kb(),
    )
    for i, msg in enumerate(msgs, 1):
        label = f"Message {i}/{len(msgs)}"
        if msg.get("media_path") and os.path.exists(msg["media_path"]):
            try:
                with open(msg["media_path"], "rb") as f:
                    await q.message.reply_photo(
                        photo=f,
                        caption=f"📸 *{label}*\n{msg.get('content') or ''}",
                        parse_mode="Markdown",
                    )
            except Exception:
                await q.message.reply_text(
                    f"📸 *{label}* _(media)_\n{msg.get('content') or ''}",
                    parse_mode="Markdown",
                )
        else:
            await q.message.reply_text(
                f"💬 *{label}*\n{msg.get('content') or '_(empty)_'}",
                parse_mode="Markdown",
            )


# ── Premium ───────────────────────────────────────────────────────────────────
async def cb_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await ensure_not_banned(update):
        return

    user_id = update.effective_user.id
    is_active = await db.check_premium_active(user_id)
    badge = await _premium_badge(user_id)

    text = (
        f"👑 *VIP PREMIUM*\n"
        f"{DIVIDER}\n"
        f"💎 Status: {badge}\n\n"
        "Choose a plan to unlock *unlimited sends*! 🚀\n\n"
        "_Prices shown in INR._"
    )
    await q.message.reply_text(text, parse_mode="Markdown", reply_markup=premium_plans_kb())


async def cb_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan_key = q.data.replace("plan_", "")
    plan = _PLANS_CACHE.get(plan_key)
    if not plan:
        await q.message.reply_text("⚠️ Plan not found.", reply_markup=back_kb())
        return

    upi = await db.get_setting("upi_id", UPI_ID)
    text = (
        f"💳 *PAYMENT — {plan['label']}*\n"
        f"{DIVIDER}\n"
        f"💰 Amount: *₹{plan['price']}*\n"
        f"📅 Duration: *{plan['days']} day(s)*\n\n"
        f"UPI ID: `{upi}`\n\n"
        "Pay via any UPI app, then tap *I've Paid* and submit your UTR/transaction ID.\n\n"
        "_Admin verifies within a few minutes._"
    )
    try:
        await q.message.reply_photo(
            photo=get_image_bytes("qr"),
            caption=text,
            parse_mode="Markdown",
            reply_markup=payment_method_kb(plan_key),
        )
    except Exception:
        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=payment_method_kb(plan_key))


async def cb_paymethod_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Creating order…")
    plan_key = q.data.replace("paymethod_admin_", "")
    await _create_order_and_send(update, ctx, plan_key, mode="admin")


async def cb_paymethod_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Creating order…")
    plan_key = q.data.replace("paymethod_auto_", "")
    await _create_order_and_send(update, ctx, plan_key, mode="auto")


async def cb_order_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Recreate a fresh order after the previous one expired."""
    q = update.callback_query
    await q.answer("Recreating order…")
    data = q.data.replace("order_retry_", "")
    # format: {plan_key}_{mode}  — split from right so plan_key with _ still works
    mode = data.rsplit("_", 1)[-1]          # "admin" or "auto"
    plan_key = data.rsplit("_", 1)[0]        # everything before last _
    await _create_order_and_send(update, ctx, plan_key, mode=mode)


async def cb_ipaid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan_key = q.data.replace("ipaid_", "")
    plan = _PLANS_CACHE.get(plan_key)
    if not plan:
        await q.message.reply_text("⚠️ Plan not found.", reply_markup=back_kb())
        return ConversationHandler.END

    user_id = update.effective_user.id
    order_id = ctx.user_data.get("active_order_id", "—")

    # Stop the countdown timer — user is submitting UTR
    _order_info.pop(user_id, None)
    old_timer = _order_timers.pop(user_id, None)
    if old_timer and not old_timer.done():
        old_timer.cancel()

    ctx.user_data["pay_plan_key"] = plan_key
    await q.message.reply_text(
        f"🔖 *Enter Your UTR / Transaction ID*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔖 Order ID: `{order_id}`\n"
        f"📦 Plan: *{plan['label']}*\n"
        f"💰 Amount: ₹{plan['price']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send your *UTR number* or *transaction ID* now:",
        parse_mode="Markdown",
    )
    return PAY_UTR


async def handle_utr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    utr = update.message.text.strip()
    user_id = update.effective_user.id
    plan_key = ctx.user_data.get("pay_plan_key", "")
    plan = _PLANS_CACHE.get(plan_key, {})

    # ── Duplicate UTR check ────────────────────────────────────────────────────
    existing = await db.get_payment_by_utr(utr)
    if existing:
        support = await db.get_setting("support_username", ADMIN_USERNAME)
        status_map = {"approved": "✅ Approved", "rejected": "❌ Rejected", "pending": "⏳ Pending"}
        prev_status = status_map.get(existing.get("status", ""), "⏳ Pending")
        prev_date = (existing.get("created_at") or "")[:16]
        await update.message.reply_text(
            f"🚫 *UTR Already Used!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 UTR: `{utr}`\n"
            f"📅 Submitted: {prev_date}\n"
            f"📌 Status: {prev_status}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"This UTR/Transaction ID has already been recorded.\n"
            f"Each transaction can only be used once.\n\n"
            f"💬 Contact @{support} if you believe this is a mistake.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

    # Re-use the pre-created payment (created when order was placed); fall back to new
    payment_id = ctx.user_data.get("active_payment_id")
    order_id = ctx.user_data.get("active_order_id")
    if payment_id:
        await db.update_payment(payment_id, utr=utr)
        payment = await db.get_payment(payment_id)
    else:
        payment = await db.create_payment(user_id, plan_key, plan.get("price", 0))
        await db.update_payment(payment["id"], utr=utr)
        payment = await db.get_payment(payment["id"])

    order_id = order_id or (payment.get("order_id") if payment else "—")
    support = await db.get_setting("support_username", ADMIN_USERNAME)

    try:
        admin_msg = (
            f"💳 *NEW PAYMENT REQUEST*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔖 Order ID: `{order_id}`\n"
            f"👤 User: `{user_id}` (@{update.effective_user.username or 'N/A'})\n"
            f"📦 Plan: *{plan.get('label', plan_key)}*\n"
            f"💰 Amount: ₹{plan.get('price', 0)}\n"
            f"🔢 UTR: `{utr}`\n"
            f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"DB Payment ID: `{payment['id'] if payment else '?'}`"
        )
        from telegram import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
        admin_kb = IKM([
            [IKB("✅ Approve", callback_data=f"pay_approve_{payment['id']}"),
             IKB("❌ Reject", callback_data=f"pay_reject_{payment['id']}")],
        ])
        if ADMIN_TG_ID:
            await ctx.bot.send_message(
                chat_id=ADMIN_TG_ID, text=admin_msg,
                parse_mode="Markdown", reply_markup=admin_kb,
            )
    except Exception:
        pass

    await update.message.reply_text(
        f"✅ *PAYMENT SUBMITTED*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔖 Order ID: `{order_id}`\n"
        f"📦 Plan: *{plan.get('label', plan_key)}*\n"
        f"💰 Amount: ₹{plan.get('price', 0)}\n"
        f"🔢 UTR: `{utr}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⏳ Waiting for admin approval.\n"
        f"💬 Contact @{support} if not approved within 30 min.",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


async def cb_ipaid_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan_key = q.data.replace("ipaid_auto_", "")
    plan = _PLANS_CACHE.get(plan_key)
    if not plan:
        await q.message.reply_text("⚠️ Plan not found.", reply_markup=back_kb())
        return ConversationHandler.END

    user_id = update.effective_user.id
    order_id = ctx.user_data.get("active_order_id", "—")

    # Stop timer
    _order_info.pop(user_id, None)
    old_timer = _order_timers.pop(user_id, None)
    if old_timer and not old_timer.done():
        old_timer.cancel()

    ctx.user_data["pay_plan_key_auto"] = plan_key
    await q.message.reply_text(
        f"⚡ *AUTO VERIFICATION — Enter UTR*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔖 Order ID: `{order_id}`\n"
        f"📦 Plan: *{plan['label']}*\n"
        f"💰 Amount: ₹{plan['price']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send your *FamPay UTR* or *transaction ID* now:",
        parse_mode="Markdown",
    )
    return PAY_UTR_AUTO


async def handle_utr_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    utr = update.message.text.strip()
    user_id = update.effective_user.id
    plan_key = ctx.user_data.get("pay_plan_key_auto", "")
    plan = _PLANS_CACHE.get(plan_key, {})
    order_id = ctx.user_data.get("active_order_id")

    wait_msg = await update.message.reply_text(
        "⏳ *Verifying payment…*\n_Checking Gmail for your transaction…_",
        parse_mode="Markdown",
    )

    # ── Duplicate UTR check ────────────────────────────────────────────────────
    dup = await db.get_payment_by_utr(utr)
    if dup:
        support = await db.get_setting("support_username", ADMIN_USERNAME)
        status_map = {"approved": "✅ Approved", "rejected": "❌ Rejected", "pending": "⏳ Pending"}
        prev_status = status_map.get(dup.get("status", ""), "⏳ Pending")
        prev_date = (dup.get("created_at") or "")[:16]
        await update.message.reply_text(
            f"🚫 *UTR Already Used!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 UTR: `{utr}`\n"
            f"📅 Submitted: {prev_date}\n"
            f"📌 Status: {prev_status}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"This UTR/Transaction ID has already been recorded.\n"
            f"Each transaction can only be used once.\n\n"
            f"💬 Contact @{support} if you believe this is a mistake.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

    verified = await asyncio.get_event_loop().run_in_executor(
        None, gmail_checker.check_fampay_payment, utr, plan.get("price", 0)
    )

    # Re-use pre-created payment if available; otherwise create
    payment_id = ctx.user_data.get("active_payment_id")
    if payment_id:
        await db.update_payment(payment_id, utr=utr)
        payment = await db.get_payment(payment_id)
    else:
        payment = await db.create_payment(user_id, plan_key, plan.get("price", 0))
        await db.update_payment(payment["id"], utr=utr)
        payment = await db.get_payment(payment["id"])

    order_id = order_id or (payment.get("order_id") if payment else "—")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    username = update.effective_user.username
    username_str = f"@{username}" if username else "—"

    if verified:
        await db.update_payment(payment["id"], status="approved", reviewed_at=datetime.now().isoformat())
        await db.set_premium(user_id, plan_key, plan.get("days", 1))
        await db.increment_plans(user_id)
        await wait_msg.edit_text(
            f"🧾 *PAYMENT RECEIPT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Status: *APPROVED*\n"
            f"🔖 Order ID: `{order_id}`\n"
            f"👤 User ID: `{user_id}`\n"
            f"📛 Username: {username_str}\n"
            f"📦 Plan: *{plan.get('label', plan_key)}*\n"
            f"⏳ Duration: *{plan.get('days', 1)} day(s)*\n"
            f"💰 Amount: *₹{plan.get('price', 0)}*\n"
            f"🔢 UTR: `{utr}`\n"
            f"📅 Date: {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 *Premium is now ACTIVE!*\n"
            f"Enjoy unlimited DMs! 🚀",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
    else:
        support = await db.get_setting("support_username", ADMIN_USERNAME)
        retry_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Retry Again", callback_data=f"autopay_retry_{plan_key}|{utr}")],
            [InlineKeyboardButton("🏠 Back to Menu", callback_data="cb_back")],
        ])
        await wait_msg.edit_text(
            f"⚠️ *PAYMENT NOT VERIFIED*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔖 Order ID: `{order_id}`\n"
            f"📦 Plan: *{plan.get('label', plan_key)}*\n"
            f"💰 Amount: ₹{plan.get('price', 0)}\n"
            f"🔢 UTR: `{utr}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Payment not found yet. Retry in 1–2 min or contact admin.\n\n"
            f"💬 Support: @{support}",
            parse_mode="Markdown",
            reply_markup=retry_kb,
        )
    return ConversationHandler.END


async def cb_autopay_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Retrying…")
    data = q.data.replace("autopay_retry_", "")
    parts = data.split("|", 1)
    plan_key = parts[0]
    utr = parts[1] if len(parts) > 1 else ""
    plan = _PLANS_CACHE.get(plan_key, {})
    user_id = update.effective_user.id

    wait_msg = await q.message.reply_text(
        "⏳ *Retrying verification…*",
        parse_mode="Markdown",
    )
    verified = await asyncio.get_event_loop().run_in_executor(
        None, gmail_checker.check_fampay_payment, utr, plan.get("price", 0)
    )

    if verified:
        payment = await db.create_payment(user_id, plan_key, plan.get("price", 0))
        await db.update_payment(payment["id"], utr=utr, status="approved",
                                reviewed_at=datetime.now().isoformat())
        await db.set_premium(user_id, plan_key, plan.get("days", 1))
        await db.increment_plans(user_id)
        support = await db.get_setting("support_username", ADMIN_USERNAME)
        await wait_msg.edit_text(
            f"🎉 *Payment Verified — Premium Activated!*\n\n"
            f"{DIVIDER}\n"
            f"📦 Plan: *{plan.get('label', plan_key)}*\n"
            f"✅ UTR Verified: `{utr}`\n"
            f"👑 Premium: *{plan.get('days', 1)} day(s)*\n"
            f"{DIVIDER}\n\n"
            "🚀 Your premium is now active — enjoy unlimited DMs!\n\n"
            f"💬 Support: @{support}",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
    else:
        support = await db.get_setting("support_username", ADMIN_USERNAME)
        retry_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Retry Again", callback_data=f"autopay_retry_{plan_key}|{utr}")],
            [InlineKeyboardButton("🏠 Back to Menu", callback_data="cb_back")],
        ])
        await wait_msg.edit_text(
            f"⚠️ *Payment Not Showing*\n\n"
            f"{DIVIDER}\n"
            f"📦 Plan: *{plan['label']}*\n"
            f"💰 Amount: ₹{plan['price']}\n"
            f"🔖 UTR: `{utr}`\n"
            f"{DIVIDER}\n\n"
            "Your payment is not showing. Please contact the admin or retry after 1–2 minutes.\n\n"
            f"💬 Support: @{support}",
            parse_mode="Markdown",
            reply_markup=retry_kb,
        )


async def cancel_autopay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


# ── Campaign ──────────────────────────────────────────────────────────────────
async def cb_campaign(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id

    if not await ensure_account(update):
        return
    if not await ensure_not_banned(update):
        return

    msgs = await db.get_user_messages(user_id)
    if not msgs:
        await q.message.reply_text(
            f"✉️ *No Message Set*\n\n"
            f"{DIVIDER}\n"
            "You haven't set a campaign message yet.\n\n"
            "Tap *Set Message* first, then start your campaign.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Set Message Now", callback_data="cb_setmsg")],
                [InlineKeyboardButton("🔙 Back", callback_data="cb_back")],
            ]),
        )
        return

    is_premium = await db.check_premium_active(user_id)
    stats = await db.get_stats(user_id) or {}
    already_sent = stats.get("total_sent", 0)

    _free_cap = await db.get_free_limit()
    if not is_premium and already_sent >= _free_cap:
        await q.message.reply_text(
            f"🚫 *Free Limit Reached*\n\n"
            f"{DIVIDER}\n"
            f"You've used all *{_free_cap}* free sends.\n\n"
            "Upgrade to *VIP Premium* for unlimited sending! 👑",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👑 Go VIP Premium", callback_data="cb_premium")],
                [InlineKeyboardButton("🔙 Back", callback_data="cb_back")],
            ]),
        )
        return

    camp = await db.get_campaign(user_id)
    if camp and camp["status"] == "running":
        pct = round(100 * camp["sent"] / camp["total"]) if camp["total"] else 0
        bar = _build_progress_bar(camp["sent"], camp["total"])
        await q.message.reply_text(
            f"⚠️ *Campaign Already Running*\n\n"
            f"{DIVIDER}\n"
            f"`[{bar}]` {pct}%\n"
            f"📨 Sent: `{camp['sent']}` / `{camp['total']}`\n"
            f"{DIVIDER}\n",
            parse_mode="Markdown",
            reply_markup=_stop_kb(),
        )
        return

    init_text = (
        f"🚀 *Campaign Launched!*\n"
        + _live_counter_ui(0, 0, 0.0, "—")
    )
    prog_msg = await q.message.reply_text(init_text, parse_mode="Markdown", reply_markup=_stop_kb())
    _progress_msg_ids[user_id] = prog_msg.message_id
    _progress_last_edit[user_id] = time.monotonic()
    _campaign_start_times[user_id] = time.monotonic()

    async def on_progress(uid, sent, total, label):
        try:
            now = time.monotonic()
            if sent not in (0, total) and now - _progress_last_edit.get(uid, 0) < 2.0:
                return
            _progress_last_edit[uid] = now
            msg_id = _progress_msg_ids.get(uid)
            if not msg_id:
                return
            elapsed = now - _campaign_start_times.get(uid, now)
            speed = sent / max(elapsed, 1)
            text = (
                f"🚀 *Campaign Running…*\n"
                + _live_counter_ui(sent, total, speed, label or "—")
            )
            await ctx.bot.edit_message_text(
                chat_id=uid, message_id=msg_id,
                text=text, parse_mode="Markdown", reply_markup=_stop_kb(),
            )
        except Exception:
            pass

    async def on_done(uid, error):
        try:
            msg_id = _progress_msg_ids.pop(uid, None)
            _progress_last_edit.pop(uid, None)
            _campaign_start_times.pop(uid, None)

            if error == "free_limit":
                camp = await db.get_campaign(uid)
                _cap = await db.get_free_limit()
                sent = camp["sent"] if camp else _cap
                done_text = (
                    f"🛑 *Free Limit Reached — Campaign Stopped!*\n\n"
                    f"{DIVIDER}\n"
                    f"📨 DMs Sent: *{sent:,}* / {_cap}\n"
                    f"🔒 Free plan limit: *{_cap} sends*\n"
                    f"{DIVIDER}\n\n"
                    f"🚀 *Want to keep going?*\n"
                    f"Upgrade to *VIP Premium* and send to unlimited contacts — no cap, ever!\n\n"
                    f"👑 Plans start at just ₹10/day."
                )
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👑 Go VIP Premium — Unlimited Sends", callback_data="cb_premium")],
                    [InlineKeyboardButton("🏠 Back to Menu", callback_data="cb_back")],
                ])
                if msg_id:
                    await ctx.bot.edit_message_text(chat_id=uid, message_id=msg_id, text=done_text, parse_mode="Markdown", reply_markup=kb)
                else:
                    await ctx.bot.send_message(chat_id=uid, text=done_text, parse_mode="Markdown", reply_markup=kb)

            elif error == "stopped":
                camp = await db.get_campaign(uid)
                sent = camp["sent"] if camp else 0
                done_text = (
                    f"⛔ *Campaign Stopped*\n\n"
                    f"{DIVIDER}\n"
                    f"Messages sent before stopping: `{sent:,}`"
                )
                if msg_id:
                    await ctx.bot.edit_message_text(chat_id=uid, message_id=msg_id, text=done_text, parse_mode="Markdown", reply_markup=main_menu_kb())
                else:
                    await ctx.bot.send_message(chat_id=uid, text=done_text, parse_mode="Markdown", reply_markup=main_menu_kb())

            elif error:
                done_text = f"❌ *Campaign Error*\n\n`{error}`"
                if msg_id:
                    await ctx.bot.edit_message_text(chat_id=uid, message_id=msg_id, text=done_text, parse_mode="Markdown", reply_markup=main_menu_kb())
                else:
                    await ctx.bot.send_message(chat_id=uid, text=done_text, parse_mode="Markdown", reply_markup=main_menu_kb())

            else:
                camp = await db.get_campaign(uid)
                total = camp["total"] if camp else 0
                sent = camp["sent"] if camp else 0
                bar = _build_progress_bar(sent, total)
                done_text = (
                    f"✅ *Campaign Complete!*\n\n"
                    f"{DIVIDER}\n"
                    f"`[{bar}]` 100%\n"
                    f"📨 Sent to *{sent:,}* DMs\n"
                    f"🎯 Out of *{total:,}* total contacts\n"
                    f"{DIVIDER}\n\n"
                    "Great job! 🚀 Start another campaign anytime."
                )
                if msg_id:
                    await ctx.bot.edit_message_text(chat_id=uid, message_id=msg_id, text=done_text, parse_mode="Markdown", reply_markup=main_menu_kb())
                else:
                    await ctx.bot.send_message(chat_id=uid, text=done_text, parse_mode="Markdown", reply_markup=main_menu_kb())

            try:
                camp = await db.get_campaign(uid)
                if camp and camp.get("sent", 0) > 0:
                    result = await db.try_complete_referral(uid)
                    if result:
                        await db.extend_premium(result["referrer_id"], result["reward_days"], "referral")
                        await ctx.bot.send_message(
                            chat_id=result["referrer_id"],
                            text=(
                                f"🎉 *Referral Completed!*\n\n"
                                f"{DIVIDER}\n"
                                f"👤 *{result['referred_name']}* just completed your referral!\n"
                                f"   ✅ Added account\n"
                                f"   ✅ Sent their first DM campaign\n\n"
                                f"🎁 *+{result['reward_days']} day(s) VIP Premium* added to your account!\n"
                                f"{DIVIDER}\n\n"
                                f"Keep sharing your referral link to earn more! 🚀"
                            ),
                            parse_mode="Markdown",
                        )
            except Exception:
                pass

        except Exception:
            pass

    await userbot.start_campaign(user_id, msgs, on_progress, on_done)


# ── Stop Campaign ─────────────────────────────────────────────────────────────
async def cb_stop_campaign(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Stopping…", show_alert=False)
    if not await ensure_account(update):
        return
    user_id = update.effective_user.id
    await userbot.cancel_campaign(user_id)
    msg_id = _progress_msg_ids.pop(user_id, None)
    _progress_last_edit.pop(user_id, None)
    camp = await db.get_campaign(user_id)
    sent = camp["sent"] if camp else 0
    text = (
        f"⛔ *Campaign Stopped*\n\n"
        f"{DIVIDER}\n"
        f"📨 Messages sent before stopping: `{sent:,}`"
    )
    try:
        if msg_id:
            await q.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())
        else:
            await q.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())
    except Exception:
        pass


# ── Set Message conversation ──────────────────────────────────────────────────
async def cb_setmsg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await ensure_account(update):
        return

    ctx.user_data["msg_count"] = 0
    user_id = update.effective_user.id
    await db.clear_messages(user_id)

    await q.message.reply_text(
        f"✉️ *COMPOSE CAMPAIGN MESSAGE*\n"
        f"{DIVIDER}\n\n"
        "Send your *text*, *link*, or *image* now.\n\n"
        "💡 *Tips:*\n"
        "• You can add multiple messages — each gets sent separately\n"
        "• Mix text and images freely\n"
        "• Keep it concise for better response rates\n\n"
        f"{DIVIDER}\n"
        "Send your first message 👇",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return SET_MSG_COLLECT


async def handle_set_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    count = ctx.user_data.get("msg_count", 0) + 1
    ctx.user_data["msg_count"] = count

    msg = update.message

    no_preview = bool(
        getattr(msg, "link_preview_options", None)
        and getattr(msg.link_preview_options, "is_disabled", False)
    )

    quoted_prefix = ""
    if msg.reply_to_message:
        quoted = msg.reply_to_message
        quoted_text = (
            quoted.text
            or quoted.caption
            or ("[media]" if quoted.photo or quoted.video or quoted.document else "")
        )
        if quoted_text:
            quoted_prefix = f"❝ {quoted_text} ❞\n\n"

    if msg.photo:
        photo = msg.photo[-1]
        file = await ctx.bot.get_file(photo.file_id)
        path = os.path.join("data", f"media_{user_id}_{count}.jpg")
        await file.download_to_drive(path)
        caption = quoted_prefix + (msg.caption or "")
        await db.add_message(user_id, content=caption, media_path=path, media_type="photo",
                             link_preview_disabled=no_preview)
        type_label = "📸 Image"

    elif msg.document:
        file = await ctx.bot.get_file(msg.document.file_id)
        path = os.path.join("data", f"media_{user_id}_{count}_{msg.document.file_name}")
        await file.download_to_drive(path)
        caption = quoted_prefix + (msg.caption or "")
        await db.add_message(user_id, content=caption, media_path=path, media_type="document",
                             link_preview_disabled=no_preview)
        type_label = "📎 File"

    else:
        text = quoted_prefix + (msg.text or "")
        await db.add_message(user_id, content=text, link_preview_disabled=no_preview)
        type_label = "💬 Text"

    preview_note = "  _(no link preview)_" if no_preview else ""
    reply_note = "  _(with quoted reply)_" if quoted_prefix else ""

    await update.message.reply_text(
        f"✅ *{type_label} saved! ({count} total)*{preview_note}{reply_note}\n\n"
        "Send another message to add more,\n"
        "or tap *Done* to finish.",
        parse_mode="Markdown",
        reply_markup=done_kb(count),
    )
    return SET_MSG_COLLECT


async def handle_msg_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    count = ctx.user_data.get("msg_count", 0)
    await q.message.reply_text(
        f"🎯 *{count} message(s) ready!*\n\n"
        f"{DIVIDER}\n"
        "Your campaign message is set.\n\n"
        "Tap *Start Mass DM Campaign* to send them to all your contacts! 🚀",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Start Campaign Now", callback_data="cb_campaign")],
            [InlineKeyboardButton("📋 Preview Messages", callback_data="cb_previewmsg")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="cb_back")],
        ]),
    )
    ctx.user_data["msg_count"] = 0
    return ConversationHandler.END


async def cancel_setmsg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


# ── Add Account conversation ──────────────────────────────────────────────────
async def cb_addaccount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    acc = await db.get_account(user_id)
    if acc:
        await q.message.reply_text(
            f"✅ *Account Already Linked*\n\n"
            f"{DIVIDER}\n"
            f"📱 Phone: `{acc['phone']}`\n\n"
            "Use *Remove Account* first if you want to switch accounts.",
            parse_mode="Markdown",
            reply_markup=back_kb(),
        )
        return ConversationHandler.END

    await q.message.reply_text(
        f"➕ *ADD YOUR TELEGRAM ACCOUNT*\n"
        f"{DIVIDER}\n\n"
        "*Step 1 of 3 — Phone Number*\n\n"
        "Enter your phone number with country code:\n"
        "Example: `+91XXXXXXXXXX`\n\n"
        "🔒 _Your account is only used to send DMs — we never access personal data._",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ADD_PHONE


async def handle_add_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    user_id = update.effective_user.id

    if not phone.startswith("+"):
        await update.message.reply_text(
            "⚠️ Include country code. Example: `+91XXXXXXXXXX`",
            parse_mode="Markdown",
        )
        return ADD_PHONE

    await update.message.reply_text(
        f"📲 *Sending OTP to {phone}…*\n\n"
        "_Please wait a moment…_",
        parse_mode="Markdown",
    )
    try:
        phone_code_hash = await userbot.send_code(user_id, phone)
    except Exception as ex:
        await update.message.reply_text(
            f"❌ *Failed to Send OTP*\n\n"
            f"{DIVIDER}\n"
            f"Error: `{ex}`\n\n"
            "Please double-check your number and try again.\n"
            "Make sure your API credentials are correct.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END

    ctx.user_data["phone"] = phone
    ctx.user_data["phone_code_hash"] = phone_code_hash
    await db.upsert_user(user_id, phone=phone, phone_code_hash=phone_code_hash, state="code")

    await update.message.reply_text(
        f"✅ *OTP Sent to {phone}!*\n\n"
        f"{DIVIDER}\n"
        "*Step 2 of 3 — Enter OTP*\n\n"
        "Check your Telegram app for the code.\n"
        "Enter it below (with or without spaces):\n"
        "Example: `1 2 3 4 5` or `12345`",
        parse_mode="Markdown",
    )
    return ADD_CODE


async def handle_add_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().replace(" ", "")
    user_id = update.effective_user.id
    phone = ctx.user_data.get("phone")
    phone_code_hash = ctx.user_data.get("phone_code_hash")

    try:
        await userbot.sign_in(user_id, phone, code, phone_code_hash)
        await db.add_account(user_id, phone)
        await db.upsert_user(user_id, state="idle")
        await db.mark_referral_account_added(user_id)
        await update.message.reply_text(
            f"🎉 *Account Added Successfully!*\n\n"
            f"{DIVIDER}\n"
            f"📱 Phone: `{phone}`\n"
            f"🆓 Free sends available: *{await db.get_free_limit()}*\n"
            f"{DIVIDER}\n\n"
            "You're all set! Tap *Set Message* to compose your first campaign. 🚀",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END
    except Exception as ex:
        if "password" in str(ex).lower() or "2fa" in str(ex).lower() or "SessionPasswordNeeded" in str(ex):
            ctx.user_data["code"] = code
            await update.message.reply_text(
                f"🔐 *2FA Password Required*\n\n"
                f"{DIVIDER}\n"
                "*Step 3 of 3 — Two-Factor Authentication*\n\n"
                "Your account has 2FA enabled.\n"
                "Enter your Telegram 2FA password:",
                parse_mode="Markdown",
            )
            return ADD_2FA
        await update.message.reply_text(
            f"❌ *Invalid OTP*\n\n"
            f"{DIVIDER}\n"
            f"Error: `{ex}`\n\n"
            "Please try again with the correct code.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return ConversationHandler.END


async def handle_add_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    user_id = update.effective_user.id
    phone = ctx.user_data.get("phone")

    try:
        await userbot.sign_in_2fa(user_id, password)
        await db.add_account(user_id, phone)
        await db.upsert_user(user_id, state="idle")
        await db.mark_referral_account_added(user_id)
        await update.message.reply_text(
            f"🎉 *Account Added Successfully!*\n\n"
            f"{DIVIDER}\n"
            f"📱 Phone: `{phone}`\n"
            f"🔐 2FA: ✅ Verified\n"
            f"🆓 Free sends: *{FREE_DM_LIMIT}*\n"
            f"{DIVIDER}\n\n"
            "You're all set! 🚀",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
    except Exception as ex:
        await update.message.reply_text(
            f"❌ *Wrong 2FA Password*\n\n"
            f"Error: `{ex}`\n\n"
            "Please try again.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
    return ConversationHandler.END


async def cancel_addaccount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


# ── Gift Code ─────────────────────────────────────────────────────────────────
async def cb_giftcode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await ensure_account(update):
        return
    if not await ensure_not_banned(update):
        return
    await q.message.reply_text(
        f"🎁 *REDEEM GIFT CODE*\n"
        f"{DIVIDER}\n\n"
        "Enter your gift code below:\n"
        "Example: `A1B2C3D4`\n\n"
        "_Gift codes grant free premium days instantly._",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return GIFT_CODE_INPUT


async def handle_gift_code_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    code = update.message.text.strip().upper()

    row = await db.use_gift_code(code, user_id)
    if not row:
        await update.message.reply_text(
            f"❌ *Invalid or Fully Used Code*\n\n"
            f"{DIVIDER}\n"
            f"Code: `{code}`\n\n"
            "This code is either invalid or has reached its maximum number of uses.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Try Again", callback_data="cb_giftcode")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="cb_back")],
            ]),
        )
        return ConversationHandler.END

    days = row["days"]
    label = "Unlimited (Lifetime)" if days >= 999 else f"{days} day(s)"
    await db.set_premium(user_id, f"gift_{days}d", days)
    await db.increment_plans(user_id)

    await update.message.reply_text(
        f"🎉 *Gift Code Redeemed!*\n\n"
        f"{DIVIDER}\n"
        f"✅ Code: `{code}`\n"
        f"👑 Premium: *{label}*\n"
        f"{DIVIDER}\n\n"
        "Your premium is now active. Enjoy unlimited DMs! 🚀",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


async def cancel_giftcode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


# ── Accept Pending Join Requests ──────────────────────────────────────────────
async def cb_acceptpending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await ensure_account(update):
        return
    if not await ensure_not_banned(update):
        return

    user_id = update.effective_user.id
    is_premium = await db.check_premium_active(user_id)
    limit_text = "Unlimited" if is_premium else f"{FREE_ACCEPT_LIMIT}"

    await q.message.reply_text(
        f"✅ *ACCEPT PENDING JOIN REQUESTS*\n\n"
        f"{DIVIDER}\n"
        f"💎 Your limit: *{limit_text} requests per use*\n"
        f"{DIVIDER}\n\n"
        "Send the channel username or link — public *and* private both work:\n\n"
        "🔓 Public: `@MyChannel` or `https://t.me/MyChannel`\n"
        "🔒 Private: `https://t.me/+InviteHash`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Cancel", callback_data="cb_back")]
        ]),
    )
    return AP_CHANNEL_INPUT


def _parse_channel_input(text: str):
    text = text.strip()
    if "t.me/+" in text or "t.me/joinchat/" in text:
        if "t.me/+" in text:
            invite_hash = text.split("t.me/+")[-1].rstrip("/").split("?")[0]
            raw = f"https://t.me/+{invite_hash}"
        else:
            invite_hash = text.split("t.me/joinchat/")[-1].rstrip("/").split("?")[0]
            raw = f"https://t.me/joinchat/{invite_hash}"
        return raw, f"+{invite_hash[:12]}…"
    if "t.me/" in text:
        username = text.split("t.me/")[-1].rstrip("/").split("?")[0]
    elif text.startswith("@"):
        username = text[1:]
    else:
        username = text
    return f"@{username}", f"@{username}"


async def handle_ap_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    channel, label = _parse_channel_input(text)
    ctx.user_data["ap_channel"] = channel
    ctx.user_data["ap_label"] = label

    wait_msg = await update.message.reply_text(
        f"⏳ Fetching pending requests for `{label}`…",
        parse_mode="Markdown",
    )

    try:
        users, total = await userbot.get_pending_join_requests(user_id, channel)
    except Exception as ex:
        await wait_msg.edit_text(
            f"❌ *Could Not Fetch Requests*\n\n"
            f"{DIVIDER}\n"
            f"Error: `{ex}`\n\n"
            "Make sure:\n"
            "• The channel link / username is correct\n"
            "• Your linked account is an admin of that channel\n"
            "• Join requests are enabled in the channel settings\n"
            "• For private channels, paste the full invite link (e.g. `https://t.me/+abc123`)",
            parse_mode="Markdown",
            reply_markup=back_kb(),
        )
        return ConversationHandler.END

    if total == 0:
        await wait_msg.edit_text(
            f"ℹ️ *No Pending Requests*\n\n"
            f"{DIVIDER}\n"
            f"Channel: `{label}`\n"
            f"Pending: *0*\n\n"
            "There are no pending join requests to accept right now.",
            parse_mode="Markdown",
            reply_markup=back_kb(),
        )
        return ConversationHandler.END

    is_premium = await db.check_premium_active(user_id)
    max_allowed = 999_999 if is_premium else FREE_ACCEPT_LIMIT
    ctx.user_data["ap_total"] = total
    ctx.user_data["ap_max"] = max_allowed

    cap = min(total, max_allowed)
    plan_note = (
        ""
        if is_premium
        else f"\n\n⚠️ Free plan: max *{FREE_ACCEPT_LIMIT}* per use. Upgrade for unlimited 👑"
    )

    await wait_msg.edit_text(
        f"📋 *Pending Requests Found!*\n\n"
        f"{DIVIDER}\n"
        f"📣 Channel: `{label}`\n"
        f"👥 Total Pending: *{total:,}*\n"
        f"💎 Your Limit: *{'Unlimited' if is_premium else FREE_ACCEPT_LIMIT}*\n"
        f"{DIVIDER}\n\n"
        f"How many requests do you want to accept?\n"
        f"Send a number between 1 and {cap:,}:{plan_note}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Cancel", callback_data="cb_back")]
        ]),
    )
    return AP_COUNT_INPUT


async def handle_ap_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    channel = ctx.user_data.get("ap_channel", "")
    label = ctx.user_data.get("ap_label", channel)
    total = ctx.user_data.get("ap_total", 0)
    max_allowed = ctx.user_data.get("ap_max", FREE_ACCEPT_LIMIT)

    try:
        count = int(text)
        if count <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Please send a valid number, e.g. `50`.",
            parse_mode="Markdown",
        )
        return AP_COUNT_INPUT

    is_premium = await db.check_premium_active(user_id)

    if count > max_allowed and not is_premium:
        await update.message.reply_text(
            f"🚫 *Free Plan Limit*\n\n"
            f"{DIVIDER}\n"
            f"You can accept up to *{FREE_ACCEPT_LIMIT}* requests on the free plan.\n\n"
            "Upgrade to *VIP Premium* for unlimited! 👑",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👑 Go VIP Premium", callback_data="cb_premium")],
                [InlineKeyboardButton("🔙 Back to Menu", callback_data="cb_back")],
            ]),
        )
        return ConversationHandler.END

    count = min(count, total, max_allowed)

    wait_msg = await update.message.reply_text(
        f"⏳ *Accepting {count:,} requests…*\n\n"
        "_Please wait, this may take a moment._",
        parse_mode="Markdown",
    )

    try:
        accepted, _ = await userbot.accept_join_requests(user_id, channel, count)
    except Exception as ex:
        await wait_msg.edit_text(
            f"❌ *Error*\n\n"
            f"{DIVIDER}\n"
            f"`{ex}`",
            parse_mode="Markdown",
            reply_markup=back_kb(),
        )
        return ConversationHandler.END

    skipped = total - count
    await wait_msg.edit_text(
        f"✅ *Done — Requests Accepted!*\n\n"
        f"{DIVIDER}\n"
        f"📣 Channel: `{label}`\n"
        f"✅ Accepted: *{accepted:,}*\n"
        f"⏭ Skipped: *{skipped:,}*\n"
        f"👥 Total Pending Was: *{total:,}*\n"
        f"{DIVIDER}\n\n"
        "All done! 🚀",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )
    return ConversationHandler.END


async def cancel_acceptpending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


# ── Join Request DM ───────────────────────────────────────────────────────────
async def cb_jr_dm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point — checks account then asks for channel link."""
    q = update.callback_query
    await q.answer()

    if not await ensure_account(update):
        return ConversationHandler.END
    if not await ensure_not_banned(update):
        return ConversationHandler.END

    # Clear any previous JR session data
    ctx.user_data.pop("jr_channel", None)
    ctx.user_data.pop("jr_label", None)
    ctx.user_data.pop("jr_count", None)
    ctx.user_data.pop("jr_messages", None)
    ctx.user_data.pop("jr_msg_count", None)

    await q.message.reply_text(
        f"📨 *JOIN REQUEST DM*\n"
        f"{DIVIDER}\n\n"
        "Send your channel link — *public* or *private* both work:\n\n"
        "🔓 *Public channel:*\n"
        "   `@MyChannel`  or  `https://t.me/MyChannel`\n\n"
        "🔒 *Private channel:*\n"
        "   `https://t.me/+InviteHash`\n\n"
        f"{DIVIDER}\n"
        "_Your linked account must be an admin of the channel._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Cancel", callback_data="cb_back")]
        ]),
    )
    return JR_CHANNEL_INPUT


async def handle_jr_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Receives channel link, fetches join request count, asks how many to DM."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    channel, label = _parse_channel_input(text)
    ctx.user_data["jr_channel"] = channel
    ctx.user_data["jr_label"] = label

    wait_msg = await update.message.reply_text(
        f"⏳ Checking join requests for `{label}`…",
        parse_mode="Markdown",
    )

    try:
        importers, total = await userbot.get_pending_join_requests(user_id, channel)
    except Exception as ex:
        await wait_msg.edit_text(
            f"❌ *Could Not Fetch Requests*\n\n"
            f"{DIVIDER}\n"
            f"Error: `{ex}`\n\n"
            "Make sure:\n"
            "• The channel link / username is correct\n"
            "• Your linked account is an admin of that channel\n"
            "• Join requests are enabled in the channel settings",
            parse_mode="Markdown",
            reply_markup=back_kb(),
        )
        return ConversationHandler.END

    if total == 0:
        await wait_msg.edit_text(
            f"ℹ️ *No Pending Join Requests*\n\n"
            f"{DIVIDER}\n"
            f"Channel: `{label}`\n\n"
            "There are no pending join requests in this channel right now.",
            parse_mode="Markdown",
            reply_markup=back_kb(),
        )
        return ConversationHandler.END

    ctx.user_data["jr_total"] = total

    await wait_msg.edit_text(
        f"✅ *Join Requests Found!*\n\n"
        f"{DIVIDER}\n"
        f"📣 Channel: `{label}`\n"
        f"👥 Pending Requests: *{total:,}*\n"
        f"{DIVIDER}\n\n"
        f"How many people do you want to send a DM to?\n\n"
        f"Send a number *(e.g. 50, 100, 200)* — up to *{total:,}*:\n\n"
        "⚠️ _Their join request will NOT be accepted or dismissed — only a DM will be sent._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Cancel", callback_data="cb_back")]
        ]),
    )
    return JR_COUNT_INPUT


async def handle_jr_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Receives count, confirms it, then asks user to write the message."""
    total = ctx.user_data.get("jr_total", 0)
    label = ctx.user_data.get("jr_label", "")

    text = update.message.text.strip()
    try:
        count = int(text)
        if count <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Please send a valid number, e.g. `50`.",
            parse_mode="Markdown",
        )
        return JR_COUNT_INPUT

    count = min(count, total)
    ctx.user_data["jr_count"] = count
    ctx.user_data["jr_messages"] = []
    ctx.user_data["jr_msg_count"] = 0

    await update.message.reply_text(
        f"✅ *Got it — {count:,} DMs will be sent!*\n\n"
        f"{DIVIDER}\n"
        f"📣 Channel: `{label}`\n"
        f"📨 Recipients: *{count:,}* pending requesters\n"
        f"{DIVIDER}\n\n"
        "Now *write the message* you want to send to them.\n\n"
        "💡 You can send *text*, *links*, or *images*.\n"
        "You can add multiple messages — each is sent separately.\n\n"
        "Send your first message 👇",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return JR_MSG_COLLECT


async def handle_jr_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Collects messages for the JR DM campaign (stored in user_data, not DB)."""
    user_id = update.effective_user.id
    count = ctx.user_data.get("jr_msg_count", 0) + 1
    ctx.user_data["jr_msg_count"] = count

    if "jr_messages" not in ctx.user_data:
        ctx.user_data["jr_messages"] = []

    msg = update.message

    no_preview = bool(
        getattr(msg, "link_preview_options", None)
        and getattr(msg.link_preview_options, "is_disabled", False)
    )

    if msg.photo:
        photo = msg.photo[-1]
        file = await ctx.bot.get_file(photo.file_id)
        path = os.path.join("data", f"jr_media_{user_id}_{count}.jpg")
        await file.download_to_drive(path)
        caption = msg.caption or ""
        ctx.user_data["jr_messages"].append({
            "content": caption,
            "media_path": path,
            "media_type": "photo",
            "link_preview_disabled": no_preview,
        })
        type_label = "📸 Image"

    elif msg.document:
        file = await ctx.bot.get_file(msg.document.file_id)
        path = os.path.join("data", f"jr_media_{user_id}_{count}_{msg.document.file_name}")
        await file.download_to_drive(path)
        caption = msg.caption or ""
        ctx.user_data["jr_messages"].append({
            "content": caption,
            "media_path": path,
            "media_type": "document",
            "link_preview_disabled": no_preview,
        })
        type_label = "📎 File"

    else:
        ctx.user_data["jr_messages"].append({
            "content": msg.text or "",
            "link_preview_disabled": no_preview,
        })
        type_label = "💬 Text"

    preview_note = "  _(no link preview)_" if no_preview else ""

    await update.message.reply_text(
        f"✅ *{type_label} saved! ({count} total)*{preview_note}\n\n"
        "Send another message to add more,\n"
        "or tap *Done* to finish.",
        parse_mode="Markdown",
        reply_markup=jr_done_kb(count),
    )
    return JR_MSG_COLLECT


async def handle_jr_msg_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User tapped Done — show the 3-button launch screen."""
    q = update.callback_query
    await q.answer()

    count = ctx.user_data.get("jr_msg_count", 0)
    jr_count = ctx.user_data.get("jr_count", 0)
    label = ctx.user_data.get("jr_label", "")

    if count == 0:
        await q.message.reply_text(
            "⚠️ You haven't added any message yet. Send at least one message first.",
            parse_mode="Markdown",
            reply_markup=jr_done_kb(0),
        )
        return JR_MSG_COLLECT

    await q.message.reply_text(
        f"🎯 *Ready to Send!*\n\n"
        f"{DIVIDER}\n"
        f"📣 Channel: `{label}`\n"
        f"👥 Recipients: *{jr_count:,}* pending requesters\n"
        f"✉️ Messages: *{count}* message(s) set\n"
        f"{DIVIDER}\n\n"
        "Choose an action below 👇",
        parse_mode="Markdown",
        reply_markup=jr_ready_kb(),
    )
    return ConversationHandler.END


async def cancel_jr_dm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Cancelled.", reply_markup=main_menu_kb())
    return ConversationHandler.END


# ── JR DM Campaign — Start ────────────────────────────────────────────────────
async def cb_jr_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Starts the JR DM campaign."""
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id

    if not await ensure_account(update):
        return
    if not await ensure_not_banned(update):
        return

    channel = ctx.user_data.get("jr_channel")
    label = ctx.user_data.get("jr_label", channel)
    jr_count = ctx.user_data.get("jr_count", 0)
    messages = ctx.user_data.get("jr_messages", [])

    if not channel or not messages:
        await q.message.reply_text(
            "⚠️ *Session Expired*\n\nPlease tap *Join Request DM* again to restart.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return

    if user_id in userbot._jr_tasks and not userbot._jr_tasks[user_id].done():
        await q.message.reply_text(
            "⚠️ A Join Request DM campaign is already running.\nStop it first before starting a new one.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⛔ Stop JR Campaign", callback_data="cb_jr_stop")],
            ]),
        )
        return

    init_text = (
        f"📨 *Join Request DM — Started!*\n"
        f"👥 Targets: *{jr_count:,}* requesters\n"
        + _live_counter_ui(0, jr_count, 0.0, "—", channel=label)
    )
    prog_msg = await q.message.reply_text(init_text, parse_mode="Markdown", reply_markup=_jr_stop_kb())
    _jr_progress_msg_ids[user_id] = prog_msg.message_id
    _jr_progress_last_edit[user_id] = time.monotonic()
    _jr_campaign_start_times[user_id] = time.monotonic()

    async def on_progress(uid, sent, total, last_label):
        try:
            now = time.monotonic()
            if sent not in (0, total) and now - _jr_progress_last_edit.get(uid, 0) < 2.0:
                return
            _jr_progress_last_edit[uid] = now
            msg_id = _jr_progress_msg_ids.get(uid)
            if not msg_id:
                return
            elapsed = now - _jr_campaign_start_times.get(uid, now)
            speed = sent / max(elapsed, 1)
            text = (
                f"📨 *Join Request DM — Running…*\n"
                + _live_counter_ui(sent, total, speed, last_label or "—", channel=label)
            )
            await ctx.bot.edit_message_text(
                chat_id=uid, message_id=msg_id,
                text=text, parse_mode="Markdown", reply_markup=_jr_stop_kb(),
            )
        except Exception:
            pass

    async def on_done(uid, error):
        try:
            msg_id = _jr_progress_msg_ids.pop(uid, None)
            _jr_progress_last_edit.pop(uid, None)
            _jr_campaign_start_times.pop(uid, None)

            if error == "no_requests":
                done_text = (
                    f"ℹ️ *No Join Requests Found*\n\n"
                    f"{DIVIDER}\n"
                    f"Channel: `{label}`\n\n"
                    "There were no pending join requests to DM."
                )
            elif error:
                done_text = (
                    f"❌ *Join Request DM Error*\n\n"
                    f"{DIVIDER}\n"
                    f"`{error}`"
                )
            else:
                done_text = (
                    f"✅ *Join Request DM Complete!*\n\n"
                    f"{DIVIDER}\n"
                    f"📣 Channel: `{label}`\n"
                    f"📨 DMs Sent: *{jr_count:,}* requesters\n"
                    f"{DIVIDER}\n\n"
                    "All messages delivered! 🚀\n"
                    "Join requests remain pending — no one was accepted or dismissed."
                )

            if msg_id:
                await ctx.bot.edit_message_text(
                    chat_id=uid, message_id=msg_id,
                    text=done_text, parse_mode="Markdown", reply_markup=main_menu_kb()
                )
            else:
                await ctx.bot.send_message(
                    chat_id=uid, text=done_text,
                    parse_mode="Markdown", reply_markup=main_menu_kb()
                )
        except Exception:
            pass

    await userbot.start_jr_campaign(user_id, channel, jr_count, messages, on_progress, on_done)


# ── JR DM Campaign — Preview ──────────────────────────────────────────────────
async def cb_jr_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Previews the messages set for the JR DM campaign."""
    q = update.callback_query
    await q.answer()

    messages = ctx.user_data.get("jr_messages", [])
    if not messages:
        await q.message.reply_text(
            "📭 *No message set.*\n\nTap *Join Request DM* again to restart.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(),
        )
        return

    await q.message.reply_text(
        f"📋 *YOUR JOIN REQUEST DM MESSAGE(S)*\n{DIVIDER}\n_{len(messages)} message(s) set:_\n",
        parse_mode="Markdown",
        reply_markup=jr_ready_kb(),
    )
    for i, msg in enumerate(messages, 1):
        label_num = f"Message {i}/{len(messages)}"
        if msg.get("media_path") and os.path.exists(msg["media_path"]):
            try:
                with open(msg["media_path"], "rb") as f:
                    await q.message.reply_photo(
                        photo=f,
                        caption=f"📸 *{label_num}*\n{msg.get('content') or ''}",
                        parse_mode="Markdown",
                    )
            except Exception:
                await q.message.reply_text(
                    f"📸 *{label_num}* _(media)_\n{msg.get('content') or ''}",
                    parse_mode="Markdown",
                )
        else:
            await q.message.reply_text(
                f"💬 *{label_num}*\n{msg.get('content') or '_(empty)_'}",
                parse_mode="Markdown",
            )


# ── JR DM Campaign — Cancel ───────────────────────────────────────────────────
async def cb_jr_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancels the JR DM setup and returns to main menu."""
    q = update.callback_query
    await q.answer()
    ctx.user_data.pop("jr_channel", None)
    ctx.user_data.pop("jr_label", None)
    ctx.user_data.pop("jr_count", None)
    ctx.user_data.pop("jr_messages", None)
    ctx.user_data.pop("jr_msg_count", None)
    await q.message.reply_text(
        "❌ *Join Request DM Cancelled*\n\nReturning to main menu.",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )


# ── JR DM Campaign — Stop ─────────────────────────────────────────────────────
async def cb_jr_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Stops a running JR DM campaign."""
    q = update.callback_query
    await q.answer("Stopping…", show_alert=False)
    user_id = update.effective_user.id
    await userbot.cancel_jr_campaign(user_id)
    msg_id = _jr_progress_msg_ids.pop(user_id, None)
    _jr_progress_last_edit.pop(user_id, None)
    text = (
        f"⛔ *Join Request DM Stopped*\n\n"
        f"{DIVIDER}\n"
        "Campaign was stopped. Some messages may have already been sent."
    )
    try:
        if msg_id:
            await q.message.edit_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())
        else:
            await q.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())
    except Exception:
        pass


# ── Remove Account ────────────────────────────────────────────────────────────
async def cb_removeaccount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    acc = await db.get_account(user_id)
    if not acc:
        await q.message.reply_text(
            "ℹ️ No account linked to remove.",
            reply_markup=back_kb(),
        )
        return

    await q.message.reply_text(
        f"⚠️ *Remove Account*\n\n"
        f"{DIVIDER}\n"
        f"📱 Phone: `{acc['phone']}`\n\n"
        "This will log out your Telegram account from this bot.\n"
        "Your session file will be deleted.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Yes, Remove Account", callback_data="cb_removeaccount_confirm")],
            [InlineKeyboardButton("🔙 Cancel", callback_data="cb_back")],
        ]),
    )


async def cb_removeaccount_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    await userbot.logout_user(user_id)
    await db.remove_account(user_id)
    await q.message.reply_text(
        f"✅ *Account Removed*\n\n"
        f"{DIVIDER}\n"
        "Your Telegram account has been unlinked.\n"
        "Tap *Add Account* to link a different account.",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(),
    )


# ── Refer & Earn ───────────────────────────────────────────────────────────────
async def cb_refer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = update.effective_user.id
    bot_username = BOT_USERNAME or "YourBot"
    referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    stats = await db.get_referrer_stats(user_id)
    reward_days = await db.get_referral_reward_days()

    earned_days = stats["completed"] * reward_days

    msg = (
        f"🔗 *REFER & EARN — Free Premium Days!*\n\n"
        f"{DIVIDER}\n"
        f"🎯 *Your Unique Referral Link:*\n"
        f"`{referral_link}`\n\n"
        f"📊 *Your Referral Stats:*\n"
        f"✅ Completed: *{stats['completed']}*\n"
        f"⏳ Pending:   *{stats['pending']}*\n"
        f"🎁 Days Earned: *{earned_days} day(s)*\n\n"
        f"{DIVIDER}\n"
        f"📋 *HOW IT WORKS:*\n\n"
        f"1️⃣ Share your link with a friend\n"
        f"2️⃣ They must open the bot using YOUR link\n"
        f"3️⃣ They must *add their Telegram account* ✅\n"
        f"4️⃣ They must *run a DM campaign & send at least 1 message* ✅\n\n"
        f"🎁 You earn *+{reward_days} day(s) VIP Premium* per referral!\n\n"
        f"{DIVIDER}\n"
        f"⚠️ *IMPORTANT RULES:*\n"
        f"• Just opening/starting the bot does NOT count\n"
        f"• Account must be added AND DMs must be sent\n"
        f"• Both steps are mandatory — no exceptions\n"
        f"• You cannot refer yourself\n"
        f"• Each person can only be referred once"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share My Referral Link", switch_inline_query=referral_link)],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="cb_back")],
    ])
    await q.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)


# ── App setup ─────────────────────────────────────────────────────────────────
def build_app():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    add_acc_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_addaccount, pattern="^cb_addaccount$")],
        states={
            ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_phone)],
            ADD_CODE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_code)],
            ADD_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cancel_addaccount)],
        allow_reentry=True, per_message=False,
    )
    setmsg_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_setmsg, pattern="^cb_setmsg$")],
        states={
            SET_MSG_COLLECT: [
                CallbackQueryHandler(handle_msg_done, pattern="^msg_done$"),
                CallbackQueryHandler(cb_back, pattern="^cb_back$"),
                MessageHandler(filters.ALL & ~filters.COMMAND, handle_set_msg),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_setmsg)],
        allow_reentry=True, per_message=False,
    )
    payment_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_ipaid, pattern="^ipaid_(?!auto_)")],
        states={
            PAY_UTR: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_utr)],
        },
        fallbacks=[CommandHandler("cancel", cancel_addaccount)],
        allow_reentry=True, per_message=False,
    )
    payment_auto_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_ipaid_auto, pattern="^ipaid_auto_")],
        states={
            PAY_UTR_AUTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_utr_auto)],
        },
        fallbacks=[CommandHandler("cancel", cancel_autopay)],
        allow_reentry=True, per_message=False,
    )
    giftcode_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_giftcode, pattern="^cb_giftcode$")],
        states={
            GIFT_CODE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_gift_code_input)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_giftcode),
            CallbackQueryHandler(cancel_giftcode, pattern="^cb_back$"),
        ],
        allow_reentry=True, per_message=False,
    )
    accept_pending_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_acceptpending, pattern="^cb_acceptpending$")],
        states={
            AP_CHANNEL_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ap_channel)],
            AP_COUNT_INPUT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ap_count)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_acceptpending),
            CallbackQueryHandler(cancel_acceptpending, pattern="^cb_back$"),
        ],
        allow_reentry=True, per_message=False,
    )

    # ── Join Request DM conversation ──────────────────────────────────────
    jr_dm_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_jr_dm, pattern="^cb_jr_dm$")],
        states={
            JR_CHANNEL_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_jr_channel),
            ],
            JR_COUNT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_jr_count),
            ],
            JR_MSG_COLLECT: [
                CallbackQueryHandler(handle_jr_msg_done, pattern="^jr_msg_done$"),
                CallbackQueryHandler(cancel_jr_dm, pattern="^cb_back$"),
                MessageHandler(filters.ALL & ~filters.COMMAND, handle_jr_msg),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_jr_dm),
            CallbackQueryHandler(cancel_jr_dm, pattern="^cb_back$"),
        ],
        allow_reentry=True, per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(add_acc_conv)
    app.add_handler(setmsg_conv)
    app.add_handler(payment_conv)
    app.add_handler(payment_auto_conv)
    app.add_handler(giftcode_conv)
    app.add_handler(accept_pending_conv)
    app.add_handler(jr_dm_conv)
    app.add_handler(CallbackQueryHandler(cb_back, pattern="^cb_back$"))
    app.add_handler(CallbackQueryHandler(cb_tutorial, pattern="^cb_tutorial$"))
    app.add_handler(CallbackQueryHandler(cb_myaccount, pattern="^cb_myaccount$"))
    app.add_handler(CallbackQueryHandler(cb_stats, pattern="^cb_stats$"))
    app.add_handler(CallbackQueryHandler(cb_previewmsg, pattern="^cb_previewmsg$"))
    app.add_handler(CallbackQueryHandler(cb_premium, pattern="^cb_premium$"))
    app.add_handler(CallbackQueryHandler(cb_plan, pattern="^plan_"))
    app.add_handler(CallbackQueryHandler(cb_paymethod_admin, pattern="^paymethod_admin_"))
    app.add_handler(CallbackQueryHandler(cb_paymethod_auto, pattern="^paymethod_auto_"))
    app.add_handler(CallbackQueryHandler(cb_autopay_retry, pattern="^autopay_retry_"))
    app.add_handler(CallbackQueryHandler(cb_order_retry, pattern="^order_retry_"))
    app.add_handler(CallbackQueryHandler(cb_check_joined, pattern="^cb_check_joined$"))
    app.add_handler(CallbackQueryHandler(cb_campaign, pattern="^cb_campaign$"))
    app.add_handler(CallbackQueryHandler(cb_stop_campaign, pattern="^cb_stop_campaign$"))
    app.add_handler(CallbackQueryHandler(cb_removeaccount, pattern="^cb_removeaccount$"))
    app.add_handler(CallbackQueryHandler(cb_removeaccount_confirm, pattern="^cb_removeaccount_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_refer, pattern="^cb_refer$"))
    app.add_handler(CallbackQueryHandler(cb_jr_start, pattern="^cb_jr_start$"))
    app.add_handler(CallbackQueryHandler(cb_jr_preview, pattern="^cb_jr_preview$"))
    app.add_handler(CallbackQueryHandler(cb_jr_cancel, pattern="^cb_jr_cancel$"))
    app.add_handler(CallbackQueryHandler(cb_jr_stop, pattern="^cb_jr_stop$"))

    return app
