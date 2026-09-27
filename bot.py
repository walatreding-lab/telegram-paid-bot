#!/usr/bin/env python3
"""
Private Telegram channel membership bot
- 1 hour free trial via unique invite link
- Reminder before trial ends
- Auto-remove if not subscribed
- Admin approve after UPI payment
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
UPI_ID = os.getenv("UPI_ID", "yourname@upi")
UPI_NAME = os.getenv("UPI_NAME", "Channel Owner")
PLAN_PRICE_INR = os.getenv("PLAN_PRICE_INR", "199")
PLAN_DAYS = int(os.getenv("PLAN_DAYS", "30"))
TRIAL_SECONDS = int(os.getenv("TRIAL_SECONDS", "3600"))
REMINDER_BEFORE_SECONDS = int(os.getenv("REMINDER_BEFORE_SECONDS", "600"))
CHANNEL_INVITE_NAME = os.getenv("CHANNEL_INVITE_NAME", "Premium Channel")

DB_PATH = Path(__file__).with_name("members.db")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("paid-bot")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                trial_used INTEGER NOT NULL DEFAULT 0,
                trial_end INTEGER,
                paid_until INTEGER,
                joined INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def fmt_time(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime(
        "%d-%m-%Y %I:%M %p"
    )


def upsert_user(user) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, status, created_at)
            VALUES (?, ?, ?, 'new', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
            """,
            (user.id, user.username, user.first_name, now_ts()),
        )
        conn.commit()


def get_user(user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()


def set_fields(user_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [user_id]
    with db() as conn:
        conn.execute(f"UPDATE users SET {cols} WHERE user_id=?", vals)
        conn.commit()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def access_active(row: sqlite3.Row | None) -> bool:
    if not row:
        return False
    t = now_ts()
    if row["paid_until"] and row["paid_until"] > t:
        return True
    if row["status"] == "trial" and row["trial_end"] and row["trial_end"] > t:
        return True
    return False


def main_keyboard(row: sqlite3.Row | None) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("💎 VVIP Plans / Subscribe", callback_data="plans")],
        [InlineKeyboardButton("📊 Mera Status", callback_data="status")],
    ]
    if row and access_active(row):
        buttons.insert(
            0, [InlineKeyboardButton("🔗 Channel Join Link", callback_data="getlink")]
        )
    return InlineKeyboardMarkup(buttons)


async def create_invite(context: ContextTypes.DEFAULT_TYPE) -> str:
    link = await context.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        name=CHANNEL_INVITE_NAME,
        member_limit=1,
        expire_date=now_ts() + 3600,
    )
    return link.invite_link


async def kick_user(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    try:
        await context.bot.ban_chat_member(CHANNEL_ID, user_id)
        await context.bot.unban_chat_member(CHANNEL_ID, user_id, only_if_banned=True)
        set_fields(user_id, joined=0)
        log.info("Removed user %s from channel", user_id)
    except TelegramError as e:
        log.warning("Could not remove %s: %s", user_id, e)


async def send_join_link(update_or_query, context, text: str) -> None:
    try:
        invite = await create_invite(context)
    except TelegramError as e:
        msg = (
            "Invite link nahi ban paya. Check karo:\n"
            "1) Bot channel me ADMIN hai\n"
            "2) Invite users permission ON hai\n"
            f"Error: {e}"
        )
        if hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.reply_text(msg)
        else:
            await update_or_query.edit_message_text(msg)
        return

    body = (
        f"{text}\n\n"
        "🔗 *Join Link* (1 baar use, 1 hour valid):\n"
        f"{invite}"
    )
    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text(
            body,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(get_user(update_or_query.effective_user.id)),
        )
    else:
        await update_or_query.edit_message_text(
            body,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(get_user(update_or_query.from_user.id)),
        )


def schedule_trial_jobs(application: Application, user_id: int, trial_end: int) -> None:
    jq = application.job_queue
    reminder_at = trial_end - REMINDER_BEFORE_SECONDS
    delay_reminder = max(10, reminder_at - now_ts())
    delay_end = max(15, trial_end - now_ts())

    jq.run_once(job_trial_reminder, when=delay_reminder, data={"user_id": user_id}, name=f"rem-{user_id}")
    jq.run_once(job_trial_end, when=delay_end, data={"user_id": user_id}, name=f"end-{user_id}")


async def job_trial_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data["user_id"]
    row = get_user(user_id)
    if not row or row["status"] != "trial":
        return
    if row["paid_until"] and row["paid_until"] > now_ts():
        return
    try:
        await context.bot.send_message(
            user_id,
            "⏰ *Trial Jaldi Khatam Ho Raha Hai!*\n\n"
            "⚠️ Abhi subscribe nahi kiya to channel access band ho jayega.\n\n"
            f"💎 *VVIP Plan:* ₹{PLAN_PRICE_INR} / {PLAN_DAYS} din\n"
            "🔥 Unlimited access + exclusive content",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💎 Subscribe Now", callback_data="plans")]]
            ),
        )
    except TelegramError:
        pass


async def job_trial_end(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data["user_id"]
    row = get_user(user_id)
    if not row:
        return
    if row["paid_until"] and row["paid_until"] > now_ts():
        return
    set_fields(user_id, status="expired")
    await kick_user(context, user_id)
    try:
        await context.bot.send_message(
            user_id,
            "⛔ *Trial Khatam Ho Gaya*\n\n"
            "Channel se access remove kar diya gaya hai.\n\n"
            f"💎 Wapas join karne ke liye *VVIP Plan* lo\n"
            f"💰 Sirf ₹{PLAN_PRICE_INR} / {PLAN_DAYS} din",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💎 VVIP Subscribe", callback_data="plans")]]
            ),
        )
    except TelegramError:
        pass


async def job_paid_end(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data["user_id"]
    row = get_user(user_id)
    if not row:
        return
    if row["paid_until"] and row["paid_until"] > now_ts():
        return
    set_fields(user_id, status="expired")
    await kick_user(context, user_id)
    try:
        await context.bot.send_message(
            user_id,
            "⛔ *Subscription Expire Ho Gayi*\n\n"
            "Channel se access remove kar diya gaya hai.\n\n"
            f"💎 *VVIP Renew* karke wapas join kar sakte ho\n"
            f"💰 Sirf ₹{PLAN_PRICE_INR} / {PLAN_DAYS} din",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("💎 VVIP Renew", callback_data="plans")]]
            ),
        )
    except TelegramError:
        pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    upsert_user(user)
    row = get_user(user.id)

    if access_active(row):
        await update.message.reply_text(
            f"👋 *Welcome back, {user.first_name}!*\n\n"
            "✅ Aapka *VVIP Access* abhi active hai.\n"
            "Neeche se channel join link le sakte ho.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(row),
        )
        return

    if row["trial_used"]:
        await update.message.reply_text(
            f"⚠️ *{user.first_name}*, aapka free trial pehle use ho chuka hai.\n\n"
            f"💎 *VVIP Plan* lo aur channel join karo\n"
            f"💰 Sirf ₹{PLAN_PRICE_INR} / {PLAN_DAYS} din",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(row),
        )
        return

    trial_end = now_ts() + TRIAL_SECONDS
    set_fields(
        user.id,
        status="trial",
        trial_used=1,
        trial_end=trial_end,
    )
    schedule_trial_jobs(context.application, user.id, trial_end)
    hours = TRIAL_SECONDS // 3600
    mins = (TRIAL_SECONDS % 3600) // 60
    await send_join_link(
        update,
        context,
        f"🎉 *Welcome to VVIP, {user.first_name}!*\n\n"
        f"✅ Aapka *Free Trial* start ho gaya\n"
        f"⏱ Duration: *{hours}h {mins}m*\n"
        f"📅 Khatam: `{fmt_time(trial_end)}`\n\n"
        "⚠️ Trial ke baad subscription lena hoga, warna access remove ho jayega.",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = get_user(update.effective_user.id)
    if not row:
        await update.message.reply_text("Pehle /start dabao.")
        return
    await update.message.reply_text(
        status_text(row),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_keyboard(row),
    )


def status_text(row: sqlite3.Row) -> str:
    access = "✅ *Active*" if access_active(row) else "❌ *Band*"
    return (
        "📊 *Aapka VVIP Status*\n\n"
        f"🆔 User ID: `{row['user_id']}`\n"
        f"📌 State: `{row['status']}`\n"
        f"🎁 Trial used: {'Haan' if row['trial_used'] else 'Nahi'}\n"
        f"⏱ Trial end: `{fmt_time(row['trial_end'])}`\n"
        f"💎 Paid until: `{fmt_time(row['paid_until'])}`\n"
        f"🔓 Access: {access}"
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user = q.from_user
    upsert_user(user)
    row = get_user(user.id)
    data = q.data

    if data == "status":
        await q.edit_message_text(
            status_text(row),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(row),
        )
        return

    if data == "getlink":
        if not access_active(row):
            await q.edit_message_text(
                "Access active nahi hai. Pehle subscribe karo.",
                reply_markup=main_keyboard(row),
            )
            return
        await send_join_link(q, context, "Aapka naya join link:")
        return

    if data == "plans":
        text = (
            "💎 *VVIP Premium Plan*\n\n"
            f"💰 *Price:* ₹{PLAN_PRICE_INR}\n"
            f"📅 *Duration:* {PLAN_DAYS} din\n"
            "🔥 Full channel access + exclusive content\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📲 *UPI ID:* `{UPI_ID}`\n"
            f"👤 *Name:* {UPI_NAME}\n"
            f"💵 *Amount:* ₹{PLAN_PRICE_INR}\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "👇 QR code scan karke pay karo\n"
            "Phir *Maine Pay Kar Diya* button dabao + screenshot bhejo."
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Maine Pay Kar Diya", callback_data="paid")],
                [InlineKeyboardButton("⬅️ Back", callback_data="status")],
            ]
        )
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

        from urllib.parse import quote
        upi_data = (
            f"upi://pay?pa={quote(UPI_ID)}"
            f"&pn={quote(UPI_NAME)}"
            f"&am={PLAN_PRICE_INR}"
            f"&cu=INR"
            f"&tn=VVIP+Subscription"
        )
        qr_url = (
            "https://api.qrserver.com/v1/create-qr-code/"
            f"?size=400x400&data={quote(upi_data)}"
        )
        try:
            await context.bot.send_photo(
                chat_id=user.id,
                photo=qr_url,
                caption=(
                    f"📲 *Scan & Pay* ₹{PLAN_PRICE_INR}\n"
                    f"UPI: `{UPI_ID}`\n\n"
                    "Payment ke baad screenshot yahin bhej dena."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError as e:
            log.warning("QR send failed: %s", e)
        return

    if data == "paid":
        set_fields(user.id, status="pending")
        await q.edit_message_text(
            "✅ *Payment Request Bhej Diya*\n\n"
            "📸 Ab payment ka *screenshot* yahin bhej do.\n"
            "Admin verify karke aapko access de dega.\n\n"
            "⏳ Thoda wait karein...",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_keyboard(row),
        )
        uname = f"@{user.username}" if user.username else user.first_name
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    "💳 *New VVIP Payment Claim*\n\n"
                    f"👤 User: {uname}\n"
                    f"🆔 ID: `{user.id}`\n"
                    f"💰 Amount: ₹{PLAN_PRICE_INR}\n\n"
                    f"✅ Approve: `/approve {user.id} {PLAN_DAYS}`\n"
                    f"❌ Reject: `/reject {user.id}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except TelegramError:
                pass
        return


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    upsert_user(user)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.forward_message(
                chat_id=admin_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
            await context.bot.send_message(
                admin_id,
                f"Screenshot from `{user.id}` ({user.first_name})\n"
                f"`/approve {user.id} {PLAN_DAYS}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass
    await update.message.reply_text(
        "✅ *Screenshot Admin Ko Mil Gaya!*\n\n"
        "⏳ Approval ka wait karein.\n"
        "Jaisi payment verify hogi, aapko channel ka link mil jayega.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 1:
        await update.message.reply_text("Use: /approve USER_ID [DAYS]")
        return
    try:
        uid = int(context.args[0])
        days = int(context.args[1]) if len(context.args) > 1 else PLAN_DAYS
    except ValueError:
        await update.message.reply_text("IDs numbers hone chahiye.")
        return

    until = now_ts() + days * 86400
    upsert_user_id(uid)
    set_fields(uid, status="paid", paid_until=until)
    context.job_queue.run_once(
        job_paid_end,
        when=max(30, until - now_ts()),
        data={"user_id": uid},
        name=f"paidend-{uid}",
    )
    try:
        invite = await create_invite(context)
        await context.bot.send_message(
            uid,
            f"✅ *VVIP Payment Approved!*\n\n"
            f"🎉 Aapka access *{days} din* ke liye active ho gaya.\n"
            f"📅 Valid till: `{fmt_time(until)}`\n\n"
            f"🔗 *Channel Join Link:*\n{invite}\n\n"
            "Welcome to VVIP family 💎",
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError as e:
        await update.message.reply_text(f"User ko message nahi gaya: {e}")
        return
    await update.message.reply_text(f"Approved {uid} till {fmt_time(until)}")


def upsert_user_id(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, status, created_at)
            VALUES (?, 'new', ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id, now_ts()),
        )
        conn.commit()


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Use: /reject USER_ID")
        return
    uid = int(context.args[0])
    try:
        await context.bot.send_message(
            uid,
            "❌ *Payment Verify Nahi Hui*\n\n"
            "Sahi amount + clear screenshot dubara bhejo.\n"
            "Phir *Maine Pay Kar Diya* button dabao.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError:
        pass
    await update.message.reply_text(f"Rejected {uid}")


async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Use: /kick USER_ID")
        return
    uid = int(context.args[0])
    set_fields(uid, status="expired", paid_until=0)
    await kick_user(context, uid)
    await update.message.reply_text(f"Removed {uid}")


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await update.message.reply_text(
        f"Aapka Telegram ID: `{u.id}`\nUsername: @{u.username or '-'}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.my_chat_member
    if cmu.chat.id != CHANNEL_ID:
        return
    log.info("Bot channel status: %s", cmu.new_chat_member.status)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    if cmu.chat.id != CHANNEL_ID:
        return
    uid = cmu.new_chat_member.user.id
    status = cmu.new_chat_member.status
    upsert_user_id(uid)
    if status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        set_fields(uid, joined=1)
    elif status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        set_fields(uid, joined=0)


async def restore_jobs(application: Application) -> None:
    t = now_ts()
    with db() a(update.effective_user.id)
    if not row:
        await update.message.reply_text("Pehle /start dabao.")
        return
    await update.message.reply_text(status_text(row), reply_markup=main_keyboard(row))


def status_text(row: sqlite3.Row) -> str:
    return (
        "👤 Status\n"
        f"User ID: {row['user_id']}\n"
        f"State: {row['status']}\n"
        f"Trial used: {'Haan' if row['trial_used'] else 'Nahi'}\n"
        f"Trial end: {fmt_time(row['trial_end'])}\n"
        f"Paid until: {fmt_time(row['paid_until'])}\n"
        f"Access: {'Active ✅' if access_active(row) else 'Band ❌'}"
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user = q.from_user
    upsert_user(user)
    row = get_user(user.id)
    data = q.data

    if data == "status":
        await q.edit_message_text(status_text(row), reply_markup=main_keyboard(row))
        return

    if data == "getlink":
        if not access_active(row):
            await q.edit_message_text(
                "Access active nahi hai. Pehle subscribe karo.",
                reply_markup=main_keyboard(row),
            )
            return
        await send_join_link(q, context, "Aapka naya join link:")
        return

    if data == "plans":
        text = (
            "📦 Subscription plan\n\n"
            f"₹{PLAN_PRICE_INR} — {PLAN_DAYS} din full channel access\n\n"
            f"UPI ID: `{UPI_ID}`\n"
            f"Name: {UPI_NAME}\n"
            f"Amount: ₹{PLAN_PRICE_INR}\n\n"
            "Pay karke neeche button dabao. Admin verify karke access dega."
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Maine pay kar diya", callback_data="paid")],
                [InlineKeyboardButton("⬅️ Back", callback_data="status")],
            ]
        )
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        return

    if data == "paid":
        set_fields(user.id, status="pending")
        await q.edit_message_text(
            "Payment request admin ko bhej di.\n"
            "Screenshot yahin bhej do (photo). Approval ke baad access mil jayega.",
            reply_markup=main_keyboard(row),
        )
        uname = f"@{user.username}" if user.username else user.first_name
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    "💳 New payment claim\n"
                    f"User: {uname}\n"
                    f"ID: `{user.id}`\n"
                    f"Amount: ₹{PLAN_PRICE_INR}\n\n"
                    f"Approve: `/approve {user.id} {PLAN_DAYS}`\n"
                    f"Reject: `/reject {user.id}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except TelegramError:
                pass
        return


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    upsert_user(user)
    caption = update.message.caption or "Payment screenshot"
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.forward_message(
                chat_id=admin_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
            await context.bot.send_message(
                admin_id,
                f"Screenshot from `{user.id}` ({user.first_name})\n"
                f"`/approve {user.id} {PLAN_DAYS}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass
    await update.message.reply_text("Screenshot admin ko mil gaya. Wait for approval.")


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 1:
        await update.message.reply_text("Use: /approve USER_ID [DAYS]")
        return
    try:
        uid = int(context.args[0])
        days = int(context.args[1]) if len(context.args) > 1 else PLAN_DAYS
    except ValueError:
        await update.message.reply_text("IDs numbers hone chahiye.")
        return

    until = now_ts() + days * 86400
    upsert_user_id(uid)
    set_fields(uid, status="paid", paid_until=until)
    context.job_queue.run_once(
        job_paid_end,
        when=max(30, until - now_ts()),
        data={"user_id": uid},
        name=f"paidend-{uid}",
    )
    try:
        invite = await create_invite(context)
        await context.bot.send_message(
            uid,
            f"✅ Payment approved.\nAccess {days} din ke liye active.\n"
            f"Valid till: {fmt_time(until)}\n\n🔗 {invite}",
        )
    except TelegramError as e:
        await update.message.reply_text(f"User ko message nahi gaya: {e}")
        return
    await update.message.reply_text(f"Approved {uid} till {fmt_time(until)}")


def upsert_user_id(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, status, created_at)
            VALUES (?, 'new', ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id, now_ts()),
        )
        conn.commit()


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Use: /reject USER_ID")
        return
    uid = int(context.args[0])
    try:
        await context.bot.send_message(
            uid,
            "❌ Payment verify nahi hui. Sahi amount + screenshot dubara bhejo.",
        )
    except TelegramError:
        pass
    await update.message.reply_text(f"Rejected {uid}")


async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Use: /kick USER_ID")
        return
    uid = int(context.args[0])
    set_fields(uid, status="expired", paid_until=0)
    await kick_user(context, uid)
    await update.message.reply_text(f"Removed {uid}")


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await update.message.reply_text(
        f"Aapka Telegram ID: `{u.id}`\nUsername: @{u.username or '-'}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track if bot is added/removed from channel."""
    cmu = update.my_chat_member
    if cmu.chat.id != CHANNEL_ID:
        return
    log.info("Bot channel status: %s", cmu.new_chat_member.status)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    if cmu.chat.id != CHANNEL_ID:
        return
    uid = cmu.new_chat_member.user.id
    status = cmu.new_chat_member.status
    upsert_user_id(uid)
    if status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        set_fields(uid, joined=1)
    elif status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        set_fields(uid, joined=0)


async def restore_jobs(application: Application) -> None:
    t = now_ts()
    with db() as conn:
        rows = conn.execute("SELECT * FROM users").fetchall()
    for row in rows:
        uid = row["user_id"]
        if row["status"] == "trial" and row["trial_end"] and row["trial_end"] > t:
            schedule_trial_jobs(application, uid, row["trial_end"])
        elif row["paid_until"] and row["paid_until"] > t:
            application.job_queue.run_once(
                job_paid_end,
                when=row["paid_until"] - t,
                data={"user_id": uid},
                name=f"paidend-{uid}",
            )
        elif row["status"] in ("trial", "paid") and not access_active(row):
            application.job_queue.run_once(
                job_trial_end,
                when=5,
                data={"user_id": uid},
                name=f"cleanup-{uid}",
            )


def main() -> None:
    if not BOT_TOKEN or CHANNEL_ID == 0 or not ADMIN_IDS:
        raise SystemExit(
            "Pehle .env bharo: BOT_TOKEN, CHANNEL_ID, ADMIN_IDS"
        )
    init_db()

    async def _post_init(application: Application) -> None:
        restore_jobs(application)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
