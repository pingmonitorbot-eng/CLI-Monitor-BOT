#!/usr/bin/env python3
"""
Controller (Render) — Telegram management + GitHub Actions orchestrator.
Worker does the actual searching on GitHub Actions.
"""
import os
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from flask import Flask
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from shared_db import get_settings, save_settings, list_panels, get_panel, col_panels, ping as mongo_ping
from panels import PANEL_TYPES, make_client, LOOKBACK_MINUTES
import github_actions as gh

try:
    from telegram import CopyTextButton
    HAS_COPY_BTN = True
except ImportError:
    HAS_COPY_BTN = False

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "10000"))
WORKER_MAX_HOURS = float(os.getenv("WORKER_MAX_HOURS", "3.5"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "5"))
GITHUB_REF = os.getenv("GITHUB_REF", "main")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("controller")

(
    ADD_PANEL_TYPE,
    ADD_PANEL_URL,
    ADD_PANEL_USER,
    ADD_PANEL_PASS,
    ADD_CLI_TEXT,
    REMOVE_CLI_TEXT,
    REMOVE_PANEL_CONFIRM,
) = range(7)


def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📋 CLI Management"), KeyboardButton("🖥 Panel Management")],
            [KeyboardButton("▶️ Monitor"), KeyboardButton("🔍 Test Search")],
            [KeyboardButton("👥 Set Group"), KeyboardButton("ℹ️ Status")],
        ],
        resize_keyboard=True,
    )


def cli_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Add CLI"), KeyboardButton("➖ Remove CLI")],
            [KeyboardButton("📜 List CLIs"), KeyboardButton("⬅️ Back")],
        ],
        resize_keyboard=True,
    )


def panel_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Add Panel"), KeyboardButton("➖ Remove Panel")],
            [KeyboardButton("📜 List Panels"), KeyboardButton("⬅️ Back")],
        ],
        resize_keyboard=True,
    )


def monitor_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("▶️ Start Monitor"), KeyboardButton("⏹ Stop Monitor")],
            [KeyboardButton("⬅️ Back")],
        ],
        resize_keyboard=True,
    )


def cancel_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel")]], resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *SMS Monitor Controller*\n\n"
        "Worker চলে GitHub Actions-এ।\n"
        "এই বট: প্যানেল/CLI ম্যানেজ + Worker অন/অফ।",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("মেইন মেনু:", reply_markup=main_menu())


async def open_cli_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 *CLI Management*", parse_mode="Markdown", reply_markup=cli_menu())


async def open_panel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🖥 *Panel Management*", parse_mode="Markdown", reply_markup=panel_menu())


async def open_monitor_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_settings()
    active = gh.has_active_run() if gh.configured() else False
    status = "🟢 flag ON" if st.get("monitor_running") else "🔴 flag OFF"
    await update.message.reply_text(
        f"▶️ *Monitor*\nDB: {status}\nGitHub worker active: {'yes' if active else 'no'}",
        parse_mode="Markdown",
        reply_markup=monitor_menu(),
    )


async def set_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        save_settings(group_chat_id=chat.id)
        await update.message.reply_text(
            f"✅ Group set: `{chat.id}`", parse_mode="Markdown", reply_markup=main_menu()
        )
    else:
        context.user_data["awaiting_group_id"] = True
        await update.message.reply_text(
            "গ্রুপে `/setgroup` দাও অথবা Chat ID পাঠাও:",
            reply_markup=cancel_kb(),
        )


async def setgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        save_settings(group_chat_id=chat.id)
        await update.message.reply_text(f"✅ Group set: `{chat.id}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("গ্রুপে দাও।")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_settings()
    panels = list_panels()
    clis = st.get("clis") or []
    active = gh.has_active_run() if gh.configured() else False
    lines = [
        "ℹ️ *Controller Status*",
        f"Monitor flag: {'🟢 ON' if st.get('monitor_running') else '🔴 OFF'}",
        f"GitHub active run: {'yes' if active else 'no'}",
        f"GitHub configured: {'yes' if gh.configured() else 'NO'}",
        f"Group: `{st.get('group_chat_id') or 'not set'}`",
        f"Panels: {len(panels)} | CLIs: {len(clis)}",
        f"Worker heartbeat: `{st.get('worker_heartbeat') or '-'}`",
        f"Last cycle: `{st.get('last_cycle_at') or '-'}`",
        f"Worker max hours: {WORKER_MAX_HOURS}",
        "",
        "*Panels:*",
    ]
    for p in panels:
        extra = f" url=`{p.get('base_url')}`" if p.get("base_url") else ""
        lines.append(f"• {p.get('name')} (`{p.get('type')}`) `{p.get('username')}`{extra}")
    if clis:
        lines.append("\n*CLIs:* " + ", ".join(f"`{c}`" for c in clis))
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu())


# ─── Panels ───
async def list_panels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    panels = list_panels()
    if not panels:
        await update.message.reply_text("কোনো প্যানেল নেই।", reply_markup=panel_menu())
        return
    lines = ["📜 *Panels:*\n"]
    for p in panels:
        lines.append(
            f"• *{p.get('name')}*\n  type:`{p.get('type')}` user:`{p.get('username')}`"
            + (f"\n  url:`{p.get('base_url')}`" if p.get("base_url") else "")
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=panel_menu())


async def add_panel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton(info["label"], callback_data=f"ptype:{key}")]
        for key, info in PANEL_TYPES.items()
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="ptype:cancel")])
    await update.message.reply_text("প্যানেল টাইপ:", reply_markup=InlineKeyboardMarkup(buttons))
    return ADD_PANEL_TYPE


async def add_panel_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "ptype:cancel":
        await q.edit_message_text("বাতিল।")
        await q.message.reply_text("Panel menu:", reply_markup=panel_menu())
        return ConversationHandler.END
    ptype = data.replace("ptype:", "")
    if ptype not in PANEL_TYPES:
        await q.edit_message_text("অজানা টাইপ।")
        return ConversationHandler.END
    context.user_data["new_panel_type"] = ptype
    label = PANEL_TYPES[ptype]["label"]
    await q.edit_message_text(f"নির্বাচিত: *{label}*", parse_mode="Markdown")
    if PANEL_TYPES[ptype].get("needs_url"):
        await q.message.reply_text(
            f"🔗 *{label}* — Panel Base URL দাও\n"
            f"উদাহরণ: `https://panel.lamix.org`",
            parse_mode="Markdown",
            reply_markup=cancel_kb(),
        )
        return ADD_PANEL_URL
    await q.message.reply_text(
        f"👤 *{label}* — Username:", parse_mode="Markdown", reply_markup=cancel_kb()
    )
    return ADD_PANEL_USER


async def add_panel_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ("❌ Cancel", "Cancel", "⬅️ Back"):
        await update.message.reply_text("বাতিল।", reply_markup=panel_menu())
        return ConversationHandler.END
    context.user_data["new_panel_url"] = text.rstrip("/")
    await update.message.reply_text("👤 Username:", reply_markup=cancel_kb())
    return ADD_PANEL_USER


async def add_panel_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ("❌ Cancel", "Cancel", "⬅️ Back"):
        await update.message.reply_text("বাতিল।", reply_markup=panel_menu())
        return ConversationHandler.END
    context.user_data["new_panel_user"] = text
    await update.message.reply_text("🔑 Password:", reply_markup=cancel_kb())
    return ADD_PANEL_PASS


async def add_panel_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ("❌ Cancel", "Cancel", "⬅️ Back"):
        await update.message.reply_text("বাতিল।", reply_markup=panel_menu())
        return ConversationHandler.END

    ptype = context.user_data.get("new_panel_type", "blue_sms")
    username = context.user_data.get("new_panel_user", "")
    password = text
    base_url = context.user_data.get("new_panel_url")
    label = PANEL_TYPES[ptype]["label"]
    existing = list(col_panels.find({"type": ptype}))
    name = label if not existing else f"{label} #{len(existing) + 1}"

    doc = {
        "name": name,
        "type": ptype,
        "username": username,
        "password": password,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if base_url:
        doc["base_url"] = base_url

    # Lamix needs Playwright browser — only available on GitHub Worker.
    # Skip live login test on Controller; save credentials for Worker.
    needs_browser = PANEL_TYPES.get(ptype, {}).get("needs_browser") or ptype == "lamix_sms"
    if needs_browser:
        if not base_url:
            await update.message.reply_text(
                "❌ Lamix-এ Base URL লাগবে।", reply_markup=panel_menu()
            )
            return ConversationHandler.END
        col_panels.insert_one(doc)
        msg = (
            f"✅ প্যানেল সেভ: *{name}*\n"
            f"⚠️ Controller-এ ব্রাউজার নেই — লগইন টেস্ট স্কিপ।\n"
            f"আসল লগইন GitHub Worker-এ হবে।"
        )
        await update.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=panel_menu(),
        )
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text("⏳ লগইন টেস্ট...")
    try:
        client_obj = make_client(doc)
        ok = await asyncio.to_thread(client_obj.login)
        if hasattr(client_obj, "close"):
            await asyncio.to_thread(client_obj.close)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=panel_menu())
        return ConversationHandler.END

    if not ok:
        await update.message.reply_text("❌ লগইন ব্যর্থ।", reply_markup=panel_menu())
        return ConversationHandler.END

    col_panels.insert_one(doc)
    await update.message.reply_text(
        f"✅ প্যানেল যোগ: *{name}*", parse_mode="Markdown", reply_markup=panel_menu()
    )
    context.user_data.clear()
    return ConversationHandler.END


async def remove_panel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    panels = list_panels()
    if not panels:
        await update.message.reply_text("কোনো প্যানেল নেই।", reply_markup=panel_menu())
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(p["name"], callback_data=f"rmpanel:{p['_id']}")]
        for p in panels
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="rmpanel:cancel")])
    await update.message.reply_text("কোন প্যানেল মুছবে?", reply_markup=InlineKeyboardMarkup(buttons))
    return REMOVE_PANEL_CONFIRM


async def remove_panel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "rmpanel:cancel":
        await q.edit_message_text("বাতিল।")
        await q.message.reply_text("Panel menu:", reply_markup=panel_menu())
        return ConversationHandler.END
    from bson import ObjectId
    pid = data.replace("rmpanel:", "")
    col_panels.delete_one({"_id": ObjectId(pid)})
    await q.edit_message_text("✅ মুছেছে।")
    await q.message.reply_text("Panel menu:", reply_markup=panel_menu())
    return ConversationHandler.END


# ─── CLIs global ───
async def list_clis_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clis = get_settings().get("clis") or []
    if not clis:
        await update.message.reply_text("কোনো CLI নেই।", reply_markup=cli_menu())
        return
    await update.message.reply_text(
        "📜 *Global CLIs:*\n" + "\n".join(f"• `{c}`" for c in clis),
        parse_mode="Markdown",
        reply_markup=cli_menu(),
    )


async def add_cli_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 CLI পাঠাও (প্রতি লাইনে একটি):", reply_markup=cancel_kb()
    )
    return ADD_CLI_TEXT


async def add_cli_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ("❌ Cancel", "Cancel", "⬅️ Back"):
        await update.message.reply_text("বাতিল।", reply_markup=cli_menu())
        return ConversationHandler.END
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    existing = set(get_settings().get("clis") or [])
    added = [c for c in lines if c not in existing]
    existing.update(lines)
    save_settings(clis=sorted(existing))
    await update.message.reply_text(
        f"✅ যোগ: {', '.join(added)}" if added else "সব আগে থেকেই আছে।",
        reply_markup=cli_menu(),
    )
    return ConversationHandler.END


async def remove_cli_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clis = get_settings().get("clis") or []
    if not clis:
        await update.message.reply_text("কোনো CLI নেই।", reply_markup=cli_menu())
        return ConversationHandler.END
    await update.message.reply_text(
        "মুছতে CLI পাঠাও:\n" + "\n".join(f"`{c}`" for c in clis),
        parse_mode="Markdown",
        reply_markup=cancel_kb(),
    )
    return REMOVE_CLI_TEXT


async def remove_cli_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ("❌ Cancel", "Cancel", "⬅️ Back"):
        await update.message.reply_text("বাতিল।", reply_markup=cli_menu())
        return ConversationHandler.END
    to_remove = {x.strip() for x in text.splitlines() if x.strip()}
    new_clis = [c for c in (get_settings().get("clis") or []) if c not in to_remove]
    save_settings(clis=new_clis)
    await update.message.reply_text(f"🗑 মুছেছে: {', '.join(to_remove)}", reply_markup=cli_menu())
    return ConversationHandler.END


# ─── Monitor / GitHub ───
async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("_busy_monitor"):
        await update.message.reply_text("⏳ আগের কমান্ড চলছে, একটু পর...", reply_markup=monitor_menu())
        return
    context.user_data["_busy_monitor"] = True
    try:
        st = get_settings()
        if not st.get("group_chat_id"):
            await update.message.reply_text("আগে 👥 Set Group করো।", reply_markup=monitor_menu())
            return
        if not list_panels():
            await update.message.reply_text("আগে প্যানেল যোগ করো।", reply_markup=monitor_menu())
            return
        if not (st.get("clis") or []):
            await update.message.reply_text("আগে CLI যোগ করো।", reply_markup=monitor_menu())
            return
        if not gh.configured():
            await update.message.reply_text(
                "❌ GitHub কনফিগ নেই। `GITHUB_TOKEN` + `GITHUB_REPO` সেট করো।",
                reply_markup=monitor_menu(),
            )
            return

        save_settings(monitor_running=True)
        await update.message.reply_text("⏳ Worker চেক/স্টার্ট...")
        try:
            active = await asyncio.wait_for(asyncio.to_thread(gh.has_active_run), timeout=20)
        except Exception:
            active = False
        if active:
            await update.message.reply_text(
                "▶️ Flag ON। Worker ইতিমধ্যে চলছে।", reply_markup=monitor_menu()
            )
            return
        try:
            ok = await asyncio.wait_for(
                asyncio.to_thread(gh.trigger_workflow, GITHUB_REF), timeout=25
            )
        except Exception as e:
            logger.error("trigger error: %s", e)
            ok = False
        if ok:
            await update.message.reply_text(
                f"▶️ Worker start request পাঠানো হয়েছে।\n"
                f"প্রতি ~{WORKER_MAX_HOURS} ঘণ্টায় auto-restart হবে।",
                reply_markup=monitor_menu(),
            )
        else:
            await update.message.reply_text(
                "⚠️ Flag ON কিন্তু workflow trigger ব্যর্থ। Secrets চেক করো।",
                reply_markup=monitor_menu(),
            )
    finally:
        context.user_data["_busy_monitor"] = False


async def stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("_busy_monitor"):
        await update.message.reply_text("⏳ আগের কমান্ড চলছে...", reply_markup=monitor_menu())
        return
    context.user_data["_busy_monitor"] = True
    try:
        save_settings(monitor_running=False)
        await update.message.reply_text("⏳ Worker বন্ধ করা হচ্ছে...")
        try:
            n = await asyncio.wait_for(
                asyncio.to_thread(gh.cancel_all_in_progress), timeout=40
            )
        except Exception as e:
            logger.error("cancel error: %s", e)
            n = -1
        await update.message.reply_text(
            f"⏹ Monitor OFF। Cancelled runs: {n}", reply_markup=monitor_menu()
        )
    finally:
        context.user_data["_busy_monitor"] = False


async def test_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_settings()
    clis = st.get("clis") or []
    panels = list_panels()
    if not panels or not clis:
        await update.message.reply_text("প্যানেল + CLI লাগবে।", reply_markup=main_menu())
        return
    await update.message.reply_text("🔍 Test (Controller থেকে, ১টা CLI)...")
    cli = clis[0]
    for p in panels:
        try:
            client = make_client(p)
            ok = await asyncio.to_thread(client.login)
            if not ok:
                await update.message.reply_text(f"❌ {p['name']}: login fail")
                continue
            items, df, dt = await asyncio.to_thread(client.search_cli, cli, LOOKBACK_MINUTES)
            ranges = [it.get("range_name") or "?" for it in items] if items else ["(none)"]
            msg = (
                f"Panel: `{p['name']}`\nCLI: `{cli}`\n"
                + "\n".join(f"• `{r}`" for r in ranges[:10])
                + f"\nTime: `{df}` → `{dt}`"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            if hasattr(client, "close"):
                await asyncio.to_thread(client.close)
        except Exception as e:
            await update.message.reply_text(f"{p.get('name')}: {e}")
        await asyncio.sleep(2)
    await update.message.reply_text("✅ Test শেষ।", reply_markup=main_menu())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("বাতিল।", reply_markup=main_menu())
    return ConversationHandler.END


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_group_id"):
        return
    text = update.message.text.strip()
    if text in ("❌ Cancel", "Cancel"):
        context.user_data.pop("awaiting_group_id", None)
        await update.message.reply_text("বাতিল।", reply_markup=main_menu())
        return
    try:
        gid = int(text)
        save_settings(group_chat_id=gid)
        context.user_data.pop("awaiting_group_id", None)
        await update.message.reply_text(f"✅ Group `{gid}`", parse_mode="Markdown", reply_markup=main_menu())
    except ValueError:
        await update.message.reply_text("সঠিক Chat ID দাও।")


async def supervisor_loop(app: Application):
    """Keep worker alive while monitor_running: restart after max hours / if dead."""
    logger.info("Supervisor started")
    while True:
        try:
            await asyncio.sleep(60)
            if not gh.configured():
                continue
            st = get_settings()
            if not st.get("monitor_running"):
                continue

            active = await asyncio.to_thread(gh.has_active_run)
            started = st.get("worker_started_at")
            need_restart = False
            reason = ""

            if not active:
                need_restart = True
                reason = "no active run"
            elif started:
                try:
                    t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) - t0 > timedelta(hours=WORKER_MAX_HOURS):
                        need_restart = True
                        reason = "max hours exceeded"
                except Exception:
                    pass

            if need_restart:
                logger.info("Supervisor restart: %s", reason)
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(gh.cancel_all_in_progress), timeout=40
                    )
                except Exception as e:
                    logger.error("supervisor cancel: %s", e)
                await asyncio.sleep(25)
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(gh.trigger_workflow, GITHUB_REF), timeout=25
                    )
                except Exception as e:
                    logger.error("supervisor trigger: %s", e)
        except Exception as e:
            logger.error("Supervisor error: %s", e)


flask_app = Flask(__name__)


@flask_app.route("/")
def health():
    return "Controller OK", 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, threaded=True)


async def post_init(app: Application):
    app.create_task(supervisor_loop(app))
    try:
        st = get_settings()
        gid = st.get("group_chat_id")
        if gid:
            await app.bot.send_message(
                chat_id=int(gid),
                text="✅ *Controller deployed successfully*",
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error("controller deploy notify: %s", e)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN required")
    try:
        mongo_ping()
        logger.info("Mongo OK")
    except Exception as e:
        raise SystemExit(f"Mongo error: {e}")

    import threading
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv_add_panel = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Add Panel$"), add_panel_start)],
        states={
            ADD_PANEL_TYPE: [CallbackQueryHandler(add_panel_type_cb, pattern=r"^ptype:")],
            ADD_PANEL_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_panel_url)],
            ADD_PANEL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_panel_user)],
            ADD_PANEL_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_panel_pass)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^(❌ Cancel|⬅️ Back)$"), cancel),
        ],
        allow_reentry=True,
    )
    conv_rm_panel = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➖ Remove Panel$"), remove_panel_start)],
        states={REMOVE_PANEL_CONFIRM: [CallbackQueryHandler(remove_panel_cb, pattern=r"^rmpanel:")]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    conv_add_cli = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Add CLI$"), add_cli_start)],
        states={ADD_CLI_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cli_text)]},
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex("^(❌ Cancel|⬅️ Back)$"), cancel)],
        allow_reentry=True,
    )
    conv_rm_cli = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➖ Remove CLI$"), remove_cli_start)],
        states={REMOVE_CLI_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_cli_text)]},
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex("^(❌ Cancel|⬅️ Back)$"), cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setgroup", setgroup_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(conv_add_panel)
    app.add_handler(conv_rm_panel)
    app.add_handler(conv_add_cli)
    app.add_handler(conv_rm_cli)
    app.add_handler(MessageHandler(filters.Regex("^📋 CLI Management$"), open_cli_menu))
    app.add_handler(MessageHandler(filters.Regex("^🖥 Panel Management$"), open_panel_menu))
    app.add_handler(MessageHandler(filters.Regex("^▶️ Monitor$"), open_monitor_menu))
    app.add_handler(MessageHandler(filters.Regex("^🔍 Test Search$"), test_search))
    app.add_handler(MessageHandler(filters.Regex("^👥 Set Group$"), set_group))
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ Status$"), status_cmd))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back$"), back_main))
    app.add_handler(MessageHandler(filters.Regex("^📜 List CLIs$"), list_clis_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📜 List Panels$"), list_panels_cmd))
    app.add_handler(MessageHandler(filters.Regex("^▶️ Start Monitor$"), start_monitor))
    app.add_handler(MessageHandler(filters.Regex("^⏹ Stop Monitor$"), stop_monitor))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    logger.info("Controller starting gh=%s", gh.configured())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
