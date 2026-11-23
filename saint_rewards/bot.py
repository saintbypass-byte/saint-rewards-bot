import asyncio
import html
import logging
import os
import random
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DATABASE_PATH", "saint_rewards.sqlite3")
OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or 0)
STARTUP_IMAGE = Path(__file__).resolve().parent.parent / "assets" / "startup.png"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("saint_rewards")


class Database:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS points (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    balance INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS giveaways (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    prize TEXT NOT NULL,
                    creator_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    winner_id INTEGER,
                    winner_name TEXT
                );
                CREATE TABLE IF NOT EXISTS entries (
                    giveaway_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    PRIMARY KEY(giveaway_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS rewards (
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    cost INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, name)
                );
                """
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def ensure_user(self, chat_id, user_id, name):
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT INTO points(chat_id,user_id,name) VALUES(?,?,?) "
                "ON CONFLICT(chat_id,user_id) DO UPDATE SET name=excluded.name",
                (chat_id, user_id, name),
            )

    def balance(self, chat_id, user_id, name):
        self.ensure_user(chat_id, user_id, name)
        with self.lock, self.connect() as db:
            return db.execute("SELECT balance FROM points WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()[0]

    def change_points(self, chat_id, user_id, name, amount):
        self.ensure_user(chat_id, user_id, name)
        with self.lock, self.connect() as db:
            db.execute("UPDATE points SET balance=MAX(0,balance+?) WHERE chat_id=? AND user_id=?", (amount, chat_id, user_id))
            return db.execute("SELECT balance FROM points WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()[0]

    def leaderboard(self, chat_id, limit=10):
        with self.lock, self.connect() as db:
            return db.execute("SELECT name,balance FROM points WHERE chat_id=? ORDER BY balance DESC, name LIMIT ?", (chat_id, limit)).fetchall()

    def create_giveaway(self, chat_id, prize, creator_id):
        with self.lock, self.connect() as db:
            cur = db.execute("INSERT INTO giveaways(chat_id,prize,creator_id,created_at) VALUES(?,?,?,?)", (chat_id, prize, creator_id, datetime.now(timezone.utc).isoformat()))
            return cur.lastrowid

    def add_entry(self, giveaway_id, user_id, name):
        with self.lock, self.connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO entries(giveaway_id,user_id,name) VALUES(?,?,?)", (giveaway_id, user_id, name))
            return cur.rowcount > 0

    def entry_count(self, giveaway_id):
        with self.lock, self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM entries WHERE giveaway_id=?", (giveaway_id,)).fetchone()[0]

    def draw(self, giveaway_id):
        with self.lock, self.connect() as db:
            giveaway = db.execute("SELECT * FROM giveaways WHERE id=? AND status='open'", (giveaway_id,)).fetchone()
            if not giveaway:
                return None, []
            entries = db.execute("SELECT user_id,name FROM entries WHERE giveaway_id=?", (giveaway_id,)).fetchall()
            if not entries:
                db.execute("UPDATE giveaways SET status='closed' WHERE id=?", (giveaway_id,))
                return giveaway, [], None
            winner = random.choice(entries)
            db.execute("UPDATE giveaways SET status='drawn',winner_id=?,winner_name=? WHERE id=?", (winner['user_id'], winner['name'], giveaway_id))
            return giveaway, entries, winner

    def rewards(self, chat_id):
        with self.lock, self.connect() as db:
            return db.execute("SELECT name,cost FROM rewards WHERE chat_id=? ORDER BY cost,name", (chat_id,)).fetchall()

    def add_reward(self, chat_id, name, cost):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO rewards(chat_id,name,cost) VALUES(?,?,?) ON CONFLICT(chat_id,name) DO UPDATE SET cost=excluded.cost", (chat_id, name, cost))

    def remove_reward(self, chat_id, name):
        with self.lock, self.connect() as db:
            return db.execute("DELETE FROM rewards WHERE chat_id=? AND name=?", (chat_id, name)).rowcount > 0

    def redeem(self, chat_id, user_id, user_name, reward_name):
        with self.lock, self.connect() as db:
            reward = db.execute("SELECT cost FROM rewards WHERE chat_id=? AND name=?", (chat_id, reward_name)).fetchone()
            if not reward:
                return None, "missing"
            self.ensure_user(chat_id, user_id, user_name)
            balance = db.execute("SELECT balance FROM points WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()[0]
            if balance < reward['cost']:
                return reward['cost'], "insufficient"
            db.execute("UPDATE points SET balance=balance-? WHERE chat_id=? AND user_id=?", (reward['cost'], chat_id, user_id))
            return reward['cost'], "ok"


db = Database(DB_PATH)


def mention(user):
    return f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (OWNER_ID and user.id == OWNER_ID):
        return bool(user and OWNER_ID and user.id == OWNER_ID)
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user.id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


async def require_admin(update, context):
    if await is_admin(update, context):
        return True
    await update.effective_message.reply_text("Admin permission is required for this command.")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎁 <b>SAINT REWARDS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Run fair giveaways, reward active members, and build community spirit.\n\n"
        "🎟️ <b>Giveaways:</b> /giveaway, /join, /draw\n"
        "⭐ <b>Points:</b> /points, /leaderboard\n"
        "🏆 <b>Rewards:</b> /rewards, /redeem\n\n"
        "Admins can award points and manage the reward shop."
    )
    keyboard = [[InlineKeyboardButton("⭐ My points", callback_data="my_points"), InlineKeyboardButton("🏆 Rewards", callback_data="rewards")]]
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))


async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    balance = db.balance(update.effective_chat.id, user.id, user.full_name)
    await update.effective_message.reply_text(f"⭐ {mention(user)} has <b>{balance}</b> points.", parse_mode=ParseMode.HTML)


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.leaderboard(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("No points have been awarded yet.")
        return
    text = ["🏆 <b>LEADERBOARD</b>", "━━━━━━━━━━━━━━━━━━"]
    text.extend(f"{i}. {html.escape(row['name'])} — <b>{row['balance']}</b> ⭐" for i, row in enumerate(rows, 1))
    await update.effective_message.reply_text("\n".join(text), parse_mode=ParseMode.HTML)


async def giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /giveaway prize name")
        return
    prize = " ".join(context.args).strip()
    giveaway_id = db.create_giveaway(update.effective_chat.id, prize, update.effective_user.id)
    keyboard = [[InlineKeyboardButton("🎟️ Join giveaway", callback_data=f"join:{giveaway_id}")]]
    await update.effective_message.reply_text(
        f"🎁 <b>GIVEAWAY #{giveaway_id}</b>\nPrize: <b>{html.escape(prize)}</b>\n\nPress the button to enter. An admin can run /draw {giveaway_id} when ready.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def draw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /draw giveaway_id")
        return
    result = db.draw(int(context.args[0]))
    if result[0] is None:
        await update.effective_message.reply_text("That giveaway does not exist or was already drawn.")
        return
    giveaway_row, entries, winner = result
    if not entries:
        await update.effective_message.reply_text(f"🎁 Giveaway #{giveaway_row['id']} had no entries and was closed.")
        return
    await update.effective_message.reply_text(
        f"🎉 <b>WINNER!</b>\nPrize: <b>{html.escape(giveaway_row['prize'])}</b>\nWinner: {html.escape(winner['name'])}\nEntries: {len(entries)}",
        parse_mode=ParseMode.HTML,
    )


async def award(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    reply = update.effective_message.reply_to_message
    if not reply or not reply.from_user or not context.args or not context.args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text("Reply to a member and use: /award points")
        return
    amount = int(context.args[0])
    new_balance = db.change_points(update.effective_chat.id, reply.from_user.id, reply.from_user.full_name, amount)
    await update.effective_message.reply_text(f"⭐ Awarded <b>{amount}</b> points to {mention(reply.from_user)}. New balance: <b>{new_balance}</b>.", parse_mode=ParseMode.HTML)


async def rewards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.rewards(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("The reward shop is empty. Admins can add rewards with /reward add name | cost.")
        return
    text = ["🏆 <b>REWARD SHOP</b>", "━━━━━━━━━━━━━━━━━━"]
    text.extend(f"• {html.escape(row['name'])} — <b>{row['cost']}</b> ⭐\n  Redeem: <code>/redeem {html.escape(row['name'])}</code>" for row in rows)
    await update.effective_message.reply_text("\n".join(text), parse_mode=ParseMode.HTML)


async def reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    raw = " ".join(context.args)
    if raw.startswith("remove "):
        name = raw[7:].strip()
        removed = db.remove_reward(update.effective_chat.id, name)
        await update.effective_message.reply_text("Reward removed." if removed else "Reward not found.")
        return
    if raw.startswith("add ") and "|" in raw:
        name, cost = [x.strip() for x in raw[4:].split("|", 1)]
        if name and cost.isdigit() and int(cost) > 0:
            db.add_reward(update.effective_chat.id, name, int(cost))
            await update.effective_message.reply_text(f"Added <b>{html.escape(name)}</b> for <b>{cost}</b> points.", parse_mode=ParseMode.HTML)
            return
    await update.effective_message.reply_text("Usage: /reward add name | cost\nOr: /reward remove name")


async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Usage: /redeem reward name")
        return
    name = " ".join(context.args).strip()
    cost, status = db.redeem(update.effective_chat.id, update.effective_user.id, update.effective_user.full_name, name)
    if status == "missing":
        await update.effective_message.reply_text("That reward is not in the shop. Use /rewards to see available rewards.")
    elif status == "insufficient":
        await update.effective_message.reply_text(f"You need {cost} points to redeem that reward.")
    else:
        await update.effective_message.reply_text(f"✅ Redemption recorded for <b>{html.escape(name)}</b>. An admin will fulfill it.", parse_mode=ParseMode.HTML)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "my_points":
        balance = db.balance(query.message.chat.id, query.from_user.id, query.from_user.full_name)
        await query.message.reply_text(f"⭐ {mention(query.from_user)} has <b>{balance}</b> points.", parse_mode=ParseMode.HTML)
    elif query.data == "rewards":
        await rewards(update, context)
    elif query.data.startswith("join:"):
        giveaway_id = int(query.data.split(":", 1)[1])
        added = db.add_entry(giveaway_id, query.from_user.id, query.from_user.full_name)
        count = db.entry_count(giveaway_id)
        await query.answer("You are entered!" if added else "You are already entered.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=query.message.reply_markup)
        except TelegramError:
            pass
        log.info("giveaway=%s user=%s entries=%s", giveaway_id, query.from_user.id, count)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path not in ("/", "/health", "/health/"):
            self.send_error(404)
            return
        body = b'{"status":"ok","service":"saint-rewards-bot"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        log.info("health %s", format % args)


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    log.info("Health endpoint listening on 0.0.0.0:%s", port)
    return server


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is missing.")
    health_server = start_health_server()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("points", points))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("giveaway", giveaway))
    app.add_handler(CommandHandler("draw", draw))
    app.add_handler(CommandHandler("award", award))
    app.add_handler(CommandHandler("rewards", rewards))
    app.add_handler(CommandHandler("reward", reward))
    app.add_handler(CommandHandler("redeem", redeem))
    app.add_handler(CallbackQueryHandler(button_handler))
    log.info("SAINT REWARDS is starting...")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        health_server.shutdown()


if __name__ == "__main__":
    main()
