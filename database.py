import aiosqlite
import hashlib
import json
import os
import secrets
import time as _time
from config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "bot.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                phone       TEXT,
                state       TEXT DEFAULT 'idle',
                phone_code_hash TEXT,
                is_banned   INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS accounts (
                user_id     INTEGER PRIMARY KEY,
                phone       TEXT NOT NULL,
                added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id              INTEGER NOT NULL,
                content              TEXT,
                media_path           TEXT,
                media_type           TEXT,
                link_preview_disabled INTEGER DEFAULT 0,
                created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                status      TEXT DEFAULT 'idle',
                total       INTEGER DEFAULT 0,
                sent        INTEGER DEFAULT 0,
                started_at  TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS premium (
                user_id     INTEGER PRIMARY KEY,
                plan_key    TEXT,
                days        INTEGER,
                starts_at   TIMESTAMP,
                expires_at  TIMESTAMP,
                is_active   INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                plan_key    TEXT NOT NULL,
                amount      INTEGER NOT NULL,
                utr         TEXT,
                status      TEXT DEFAULT 'pending',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS gift_codes (
                code        TEXT PRIMARY KEY,
                days        INTEGER NOT NULL,
                max_uses    INTEGER DEFAULT 1,
                used        INTEGER DEFAULT 0,
                used_by     INTEGER,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS stats (
                user_id     INTEGER PRIMARY KEY,
                total_sent  INTEGER DEFAULT 0,
                plans_bought INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                key         TEXT PRIMARY KEY,
                value       TEXT
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id     INTEGER PRIMARY KEY,
                added_by    INTEGER,
                added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS custom_buttons (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT NOT NULL,
                url         TEXT NOT NULL,
                position    INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS referrals (
                referred_user_id  INTEGER PRIMARY KEY,
                referrer_id       INTEGER NOT NULL,
                status            TEXT DEFAULT 'pending',
                account_added     INTEGER DEFAULT 0,
                dm_sent           INTEGER DEFAULT 0,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at      TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_private_joins (
                user_id      INTEGER NOT NULL,
                channel_link TEXT NOT NULL,
                joined_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, channel_link)
            );
        """)
        await db.commit()
        # Migrations for existing databases
        for migration in [
            "ALTER TABLE gift_codes ADD COLUMN max_uses INTEGER DEFAULT 1",
            "ALTER TABLE messages ADD COLUMN link_preview_disabled INTEGER DEFAULT 0",
            "ALTER TABLE payments ADD COLUMN order_id TEXT",
        ]:
            try:
                await db.execute(migration)
                await db.commit()
            except Exception:
                pass


async def _fetchone(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def _fetchall(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def _execute(query, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()


# ── Users ─────────────────────────────────────────────────────────────────────
async def get_user(user_id):
    return await _fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))


async def upsert_user(user_id, **fields):
    existing = await get_user(user_id)
    if existing:
        if not fields:
            return
        set_clause = ", ".join(f"{k}=?" for k in fields)
        await _execute(f"UPDATE users SET {set_clause} WHERE user_id=?",
                       list(fields.values()) + [user_id])
    else:
        fields["user_id"] = user_id
        cols = ", ".join(fields.keys())
        ph = ", ".join("?" for _ in fields)
        await _execute(f"INSERT INTO users ({cols}) VALUES ({ph})", list(fields.values()))


async def get_all_users():
    return await _fetchall("SELECT * FROM users ORDER BY created_at DESC")


async def ban_user(user_id, banned=True):
    await _execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, user_id))


# ── Accounts ──────────────────────────────────────────────────────────────────
async def get_account(user_id):
    return await _fetchone("SELECT * FROM accounts WHERE user_id=?", (user_id,))


async def add_account(user_id, phone):
    existing = await get_account(user_id)
    if existing:
        await _execute("UPDATE accounts SET phone=?, added_at=CURRENT_TIMESTAMP WHERE user_id=?",
                       (phone, user_id))
    else:
        await _execute("INSERT INTO accounts (user_id, phone) VALUES (?,?)", (user_id, phone))


async def remove_account(user_id):
    await _execute("DELETE FROM accounts WHERE user_id=?", (user_id,))


# ── Messages ──────────────────────────────────────────────────────────────────
async def get_user_messages(user_id):
    return await _fetchall("SELECT * FROM messages WHERE user_id=? ORDER BY id", (user_id,))


async def add_message(user_id, content=None, media_path=None, media_type=None, link_preview_disabled=False):
    await _execute(
        "INSERT INTO messages (user_id, content, media_path, media_type, link_preview_disabled) VALUES (?,?,?,?,?)",
        (user_id, content, media_path, media_type, 1 if link_preview_disabled else 0)
    )


async def clear_messages(user_id):
    await _execute("DELETE FROM messages WHERE user_id=?", (user_id,))


# ── Campaigns ─────────────────────────────────────────────────────────────────
async def get_campaign(user_id):
    return await _fetchone(
        "SELECT * FROM campaigns WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))


async def create_campaign(user_id):
    await _execute(
        "INSERT INTO campaigns (user_id, status, started_at) VALUES (?,?,CURRENT_TIMESTAMP)",
        (user_id, "running"))
    return await get_campaign(user_id)


async def update_campaign(user_id, **fields):
    camp = await get_campaign(user_id)
    if not camp:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    await _execute(
        f"UPDATE campaigns SET {set_clause}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        list(fields.values()) + [camp["id"]])


# ── Premium ───────────────────────────────────────────────────────────────────
async def get_premium(user_id):
    return await _fetchone("SELECT * FROM premium WHERE user_id=?", (user_id,))


async def set_premium(user_id, plan_key, days):
    await _execute("""
        INSERT INTO premium (user_id, plan_key, days, starts_at, expires_at, is_active)
        VALUES (?, ?, ?,
            CURRENT_TIMESTAMP,
            datetime(CURRENT_TIMESTAMP, '+' || ? || ' days'),
            1)
        ON CONFLICT(user_id) DO UPDATE SET
            plan_key=excluded.plan_key,
            days=excluded.days,
            starts_at=excluded.starts_at,
            expires_at=excluded.expires_at,
            is_active=1
    """, (user_id, plan_key, days, days))


async def check_premium_active(user_id):
    row = await _fetchone(
        "SELECT * FROM premium WHERE user_id=? AND is_active=1 AND expires_at > CURRENT_TIMESTAMP",
        (user_id,))
    return row is not None


# ── Payments ──────────────────────────────────────────────────────────────────
def gen_order_id(user_id: int) -> str:
    """Generate a unique, short Order ID like ORD-3AF8B92C."""
    raw = f"{user_id}-{int(_time.time())}-{secrets.token_hex(8)}"
    return "ORD-" + hashlib.sha256(raw.encode()).hexdigest()[:8].upper()


async def create_payment(user_id, plan_key, amount, order_id: str | None = None):
    oid = order_id or gen_order_id(user_id)
    await _execute(
        "INSERT INTO payments (user_id, plan_key, amount, order_id) VALUES (?,?,?,?)",
        (user_id, plan_key, amount, oid))
    return await _fetchone(
        "SELECT * FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))


async def get_payment_by_order_id(order_id: str):
    return await _fetchone("SELECT * FROM payments WHERE order_id=?", (order_id,))


async def get_payment_by_utr(utr: str):
    """Return any existing payment that used this UTR/transaction ID (for duplicate check)."""
    return await _fetchone(
        "SELECT * FROM payments WHERE utr=? ORDER BY id DESC LIMIT 1", (utr,))


async def update_payment(payment_id, **fields):
    set_clause = ", ".join(f"{k}=?" for k in fields)
    await _execute(
        f"UPDATE payments SET {set_clause} WHERE id=?",
        list(fields.values()) + [payment_id])


async def get_pending_payments():
    return await _fetchall(
        "SELECT p.*, u.username FROM payments p LEFT JOIN users u ON p.user_id=u.user_id "
        "WHERE p.status='pending' ORDER BY p.created_at")


async def get_all_payments():
    return await _fetchall(
        "SELECT p.*, u.username FROM payments p LEFT JOIN users u ON p.user_id=u.user_id "
        "ORDER BY p.created_at DESC")


async def get_payment(payment_id):
    return await _fetchone("SELECT * FROM payments WHERE id=?", (payment_id,))


# ── Stats ─────────────────────────────────────────────────────────────────────
async def get_stats(user_id):
    return await _fetchone("SELECT * FROM stats WHERE user_id=?", (user_id,))


async def increment_sent(user_id, count=1):
    existing = await get_stats(user_id)
    if existing:
        await _execute("UPDATE stats SET total_sent=total_sent+? WHERE user_id=?", (count, user_id))
    else:
        await _execute("INSERT INTO stats (user_id, total_sent) VALUES (?,?)", (user_id, count))


async def increment_plans(user_id):
    existing = await get_stats(user_id)
    if existing:
        await _execute("UPDATE stats SET plans_bought=plans_bought+1 WHERE user_id=?", (user_id,))
    else:
        await _execute("INSERT INTO stats (user_id, plans_bought) VALUES (?,1)", (user_id,))


# ── Gift codes ────────────────────────────────────────────────────────────────
async def create_gift_code(code, days, max_uses=1):
    await _execute(
        "INSERT INTO gift_codes (code, days, max_uses) VALUES (?,?,?)",
        (code, days, max_uses)
    )


async def use_gift_code(code, user_id):
    row = await _fetchone(
        "SELECT * FROM gift_codes WHERE code=? AND used < max_uses", (code,))
    if not row:
        return None
    await _execute(
        "UPDATE gift_codes SET used=used+1, used_by=? WHERE code=?", (user_id, code))
    return row


# ── Custom Buttons ────────────────────────────────────────────────────────────
async def get_custom_buttons() -> list:
    return await _fetchall(
        "SELECT * FROM custom_buttons ORDER BY position ASC, id ASC")


async def add_custom_button(label: str, url: str) -> dict:
    await _execute(
        "INSERT INTO custom_buttons (label, url, position) VALUES (?,?,?)",
        (label, url, int(1e9))
    )
    return await _fetchone(
        "SELECT * FROM custom_buttons ORDER BY id DESC LIMIT 1")


async def remove_custom_button(button_id: int):
    await _execute("DELETE FROM custom_buttons WHERE id=?", (button_id,))


# ── Settings ──────────────────────────────────────────────────────────────────
async def get_setting(key: str, default=None):
    row = await _fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


async def set_setting(key: str, value: str):
    await _execute("""
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))


# ── Plans (DB-backed, falls back to config defaults) ──────────────────────────
async def get_plans() -> dict:
    """Return active plans dict. Falls back to config.PLANS if none saved in DB."""
    from config import PLANS as _DEF
    raw = await get_setting("plans", None)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {k: dict(v) for k, v in _DEF.items()}


async def save_plans(plans: dict):
    """Persist plans dict to the settings table."""
    await set_setting("plans", json.dumps(plans))


async def get_force_join_channels() -> list:
    raw = await get_setting("force_join_channels", "[]")
    try:
        return json.loads(raw)
    except Exception:
        return []


def _is_private_channel_link(text: str) -> bool:
    """Return True if the text looks like a private Telegram invite link."""
    return ("t.me/+" in text or "t.me/joinchat/" in text
            or text.startswith("https://t.me/+") or "joinchat" in text)


def normalize_channel_input(text: str) -> str:
    """
    Normalize admin input into a stored key.
    Private invite links → kept as full https:// URL.
    Public usernames    → stripped of @ and spaces.
    """
    text = text.strip()
    # Ensure invite links always start with https://
    if _is_private_channel_link(text):
        if not text.startswith("http"):
            text = "https://" + text
        return text
    # Public username: strip leading @
    return text.lstrip("@")


async def add_force_join_channel(channel: str):
    channel = normalize_channel_input(channel)
    channels = await get_force_join_channels()
    if channel and channel not in channels:
        channels.append(channel)
        await set_setting("force_join_channels", json.dumps(channels))
    return channels


async def remove_force_join_channel_by_index(index: int):
    """Remove the channel at position `index` in the list."""
    channels = await get_force_join_channels()
    if 0 <= index < len(channels):
        channels.pop(index)
        await set_setting("force_join_channels", json.dumps(channels))
    return channels


async def remove_force_join_channel(channel: str):
    """Legacy: remove by exact string match (kept for compatibility)."""
    channels = await get_force_join_channels()
    channels = [c for c in channels if c.lower() != channel.lower()]
    await set_setting("force_join_channels", json.dumps(channels))
    return channels


# ── Private-channel join tracking (trust-based, bot can't verify otherwise) ────
async def mark_private_joined(user_id: int, channel_link: str):
    await _execute("""
        INSERT INTO user_private_joins (user_id, channel_link)
        VALUES (?, ?)
        ON CONFLICT DO NOTHING
    """, (user_id, channel_link))


async def get_user_private_joins(user_id: int) -> set:
    rows = await _fetchall(
        "SELECT channel_link FROM user_private_joins WHERE user_id=?", (user_id,))
    return {r["channel_link"] for r in rows}


# ── Admins ────────────────────────────────────────────────────────────────────
async def get_extra_admins() -> list:
    rows = await _fetchall("SELECT user_id, added_at FROM admins ORDER BY added_at")
    return rows


async def add_extra_admin(user_id: int, added_by: int):
    await _execute("""
        INSERT INTO admins (user_id, added_by) VALUES (?, ?)
        ON CONFLICT(user_id) DO NOTHING
    """, (user_id, added_by))


async def remove_extra_admin(user_id: int):
    await _execute("DELETE FROM admins WHERE user_id=?", (user_id,))


async def is_extra_admin(user_id: int) -> bool:
    row = await _fetchone("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
    return row is not None


# ── Referrals ──────────────────────────────────────────────────────────────────
async def set_referral(referred_user_id: int, referrer_id: int):
    """Record a referral. Silently ignored if this user was already referred."""
    await _execute("""
        INSERT INTO referrals (referred_user_id, referrer_id)
        VALUES (?, ?)
        ON CONFLICT(referred_user_id) DO NOTHING
    """, (referred_user_id, referrer_id))


async def get_referral_by_referred(referred_user_id: int):
    return await _fetchone(
        "SELECT * FROM referrals WHERE referred_user_id=?", (referred_user_id,))


async def get_referrer_stats(referrer_id: int) -> dict:
    rows = await _fetchall(
        "SELECT status FROM referrals WHERE referrer_id=?", (referrer_id,))
    completed = sum(1 for r in rows if r["status"] == "completed")
    pending   = sum(1 for r in rows if r["status"] != "completed")
    return {"completed": completed, "pending": pending, "total": len(rows)}


async def mark_referral_account_added(referred_user_id: int):
    """Call after a referred user successfully adds their Telegram account."""
    await _execute("""
        UPDATE referrals SET account_added=1
        WHERE referred_user_id=? AND status='pending'
    """, (referred_user_id,))


async def try_complete_referral(referred_user_id: int) -> dict | None:
    """
    Atomically complete the referral if:
      - This user was referred by someone
      - They have already added an account (account_added=1)
      - The referral hasn't been completed yet (status='pending', dm_sent=0)

    Returns a dict with referrer_id and reward_days on success, None otherwise.
    """
    row = await _fetchone(
        "SELECT * FROM referrals WHERE referred_user_id=? AND status='pending' AND account_added=1 AND dm_sent=0",
        (referred_user_id,))
    if not row:
        return None

    reward_days = await get_referral_reward_days()
    await _execute("""
        UPDATE referrals
        SET status='completed', dm_sent=1, completed_at=CURRENT_TIMESTAMP
        WHERE referred_user_id=? AND status='pending'
    """, (referred_user_id,))

    # Fetch referred user info for notification message
    referred = await _fetchone("SELECT username FROM users WHERE user_id=?", (referred_user_id,))
    referred_name = f"@{referred['username']}" if referred and referred.get("username") else f"User {referred_user_id}"

    return {
        "referrer_id": row["referrer_id"],
        "reward_days":  reward_days,
        "referred_name": referred_name,
    }


async def get_all_referrals_admin() -> list:
    return await _fetchall("""
        SELECT r.*, u1.username AS referred_username, u2.username AS referrer_username
        FROM referrals r
        LEFT JOIN users u1 ON r.referred_user_id = u1.user_id
        LEFT JOIN users u2 ON r.referrer_id      = u2.user_id
        ORDER BY r.created_at DESC
    """)


async def get_referral_reward_days() -> int:
    val = await get_setting("referral_reward_days", "1")
    try:
        return max(1, int(val))
    except Exception:
        return 1


async def set_referral_reward_days(days: int):
    await set_setting("referral_reward_days", str(days))


async def extend_premium(user_id: int, days: int, plan_key: str = "referral"):
    """Add `days` to the user's current premium, or create a new one if none active."""
    existing = await _fetchone(
        "SELECT * FROM premium WHERE user_id=? AND is_active=1 AND expires_at > CURRENT_TIMESTAMP",
        (user_id,))
    if existing:
        await _execute("""
            UPDATE premium
            SET expires_at = datetime(expires_at, '+' || ? || ' days'),
                days       = days + ?
            WHERE user_id=?
        """, (days, days, user_id))
    else:
        await set_premium(user_id, plan_key, days)


# ── Dynamic Free Limit ────────────────────────────────────────────────────────
async def get_free_limit() -> int:
    """Return the current free DM limit (admin-configurable, fallback 100)."""
    val = await get_setting("free_dm_limit", "100")
    try:
        return max(1, int(val))
    except Exception:
        return 100


async def set_free_limit(n: int):
    await set_setting("free_dm_limit", str(max(1, n)))


# ── Advanced Admin Helpers ────────────────────────────────────────────────────
async def get_all_premium_users() -> list:
    """Return all users with currently active premium, sorted by expiry."""
    return await _fetchall("""
        SELECT p.user_id, p.plan_key, p.days, p.expires_at,
               u.username
        FROM premium p
        LEFT JOIN users u ON p.user_id = u.user_id
        WHERE p.is_active = 1 AND p.expires_at > CURRENT_TIMESTAMP
        ORDER BY p.expires_at ASC
    """)


async def get_accounts_count() -> int:
    """Total number of Telegram accounts linked across all users."""
    row = await _fetchone("SELECT COUNT(*) AS cnt FROM accounts")
    return row["cnt"] if row else 0


async def get_total_dms_sent() -> int:
    """Sum of all DMs sent by all users across all time."""
    row = await _fetchone("SELECT SUM(total_sent) AS total FROM stats")
    return int(row["total"]) if row and row["total"] else 0


async def get_active_campaigns_count() -> int:
    """Number of currently running campaigns."""
    row = await _fetchone(
        "SELECT COUNT(*) AS cnt FROM campaigns WHERE status='running'")
    return row["cnt"] if row else 0


async def get_new_users_today() -> int:
    """Users who joined today."""
    row = await _fetchone(
        "SELECT COUNT(*) AS cnt FROM users WHERE DATE(created_at) = DATE('now')")
    return row["cnt"] if row else 0


async def revoke_premium(user_id: int):
    """Immediately expire a user's premium (sets is_active=0, expires_at=now)."""
    await _execute(
        "UPDATE premium SET is_active=0, expires_at=CURRENT_TIMESTAMP WHERE user_id=?",
        (user_id,))


async def reset_user_sends(user_id: int):
    """Reset a user's total_sent counter back to zero."""
    await _execute(
        "UPDATE stats SET total_sent=0 WHERE user_id=?", (user_id,))
