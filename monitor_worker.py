#!/usr/bin/env python3
"""
GitHub Actions worker — monitors all panels, posts to Telegram.
Exits after WORKER_MAX_HOURS so Controller can restart the workflow.
"""
import os
import sys
import time
import logging
import traceback
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import requests

from shared_db import get_settings, save_settings, list_panels, ping
from panels import make_client, LOOKBACK_MINUTES, PANEL_TYPES

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("worker")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "5"))
WORKER_MAX_HOURS = float(os.getenv("WORKER_MAX_HOURS", "3.5"))
GITHUB_RUN_ID = os.getenv("GITHUB_RUN_ID", "")


def tg_send(chat_id: int, text: str, reply_markup: dict = None):
    if not BOT_TOKEN or not chat_id:
        return
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=30,
        )
        if r.status_code != 200:
            # retry without markdown
            payload.pop("parse_mode", None)
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=payload,
                timeout=30,
            )
    except Exception as e:
        logger.error("tg_send: %s", e)


def fmt_time(s: str) -> str:
    if not s:
        return s
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
    return s


def format_alert(panel_name: str, cli: str, ranges: List[str], df: str, dt: str) -> str:
    lines = [
        "🆕 *New Range Found*",
        "━━━━━━━━━━━━━━",
        f"🖥 *Panel:* `{panel_name}`",
        f"🔖 *CLI:* `{cli}`",
        f"📦 *Ranges:* `{len(ranges)}`",
        "",
        "📋 *Range List:*",
    ]
    for r in ranges:
        lines.append(f"• `{r}`")
    lines.append("")
    lines.append("🕒 *Searched Time:*")
    lines.append(f"`{fmt_time(df)}` → `{fmt_time(dt)}`")
    return "\n".join(lines)


def build_copy_keyboard(ranges: List[str]) -> Optional[dict]:
    if not ranges:
        return None
    rows = []
    for name in ranges[:20]:
        label = name if len(name) <= 40 else name[:37] + "..."
        # CopyTextButton style via API
        rows.append([{
            "text": f"📋 {label}",
            "copy_text": {"text": name},
        }])
    return {"inline_keyboard": rows}


def heartbeat():
    save_settings(
        worker_heartbeat=datetime.now(timezone.utc).isoformat(),
        worker_run_id=GITHUB_RUN_ID or "",
    )


def main():
    logger.info("Worker starting max_hours=%.2f", WORKER_MAX_HOURS)
    try:
        ping()
    except Exception as e:
        logger.error("Mongo ping failed: %s", e)
        sys.exit(1)

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing")
        sys.exit(1)

    started = time.time()
    max_seconds = WORKER_MAX_HOURS * 3600
    save_settings(
        worker_started_at=datetime.now(timezone.utc).isoformat(),
        worker_run_id=GITHUB_RUN_ID or "",
    )

    client_cache = {}
    cursor = 0
    cycle_idx = 0

    while True:
        if time.time() - started >= max_seconds:
            logger.info("Max runtime reached — exiting for Controller restart")
            break

        st = get_settings()
        if not st.get("monitor_running"):
            logger.info("monitor_running=False — exit")
            break

        group_id = st.get("group_chat_id")
        clis = st.get("clis") or []
        panels = list_panels()

        if not clis or not panels:
            logger.info("No CLIs or panels — sleep")
            heartbeat()
            time.sleep(CHECK_INTERVAL)
            continue

        if cursor >= len(clis):
            # full cycle complete
            cycle_idx += 1
            stats = {
                "cycle": cycle_idx,
                "clis": len(clis),
                "panels": len(panels),
                "at": datetime.now(timezone.utc).isoformat(),
            }
            save_settings(last_cycle_at=stats["at"], last_cycle_stats=stats)
            if group_id:
                tg_send(
                    int(group_id),
                    "✅ *Search cycle complete*\n"
                    f"CLIs: `{len(clis)}` | Panels: `{len(panels)}` | Cycle: `{cycle_idx}`\n"
                    "Next cycle starting...",
                )
            cursor = 0

        cli = clis[cursor]
        cursor += 1
        heartbeat()
        logger.info("CLI=%s on %s panels", cli, len(panels))

        for p in panels:
            try:
                pid = str(p["_id"])
                client = client_cache.get(pid)
                if client is None:
                    client = make_client(p)
                    client_cache[pid] = client
                if not client.ensure_login():
                    logger.error("Login fail: %s", p.get("name"))
                    client_cache.pop(pid, None)
                    continue
                items, df, dt = client.search_cli(cli, LOOKBACK_MINUTES)
                if not items:
                    continue
                ranges = [it.get("range_name") or "?" for it in items]
                msg = format_alert(p.get("name"), cli, ranges, df, dt)
                kb = build_copy_keyboard(ranges)
                if group_id:
                    tg_send(int(group_id), msg, reply_markup=kb)
            except Exception as e:
                logger.error("Panel %s error: %s", p.get("name"), e)
                logger.debug(traceback.format_exc())
                client_cache.pop(str(p.get("_id")), None)

        time.sleep(CHECK_INTERVAL)

    # cleanup playwright clients
    for c in client_cache.values():
        if hasattr(c, "close"):
            try:
                c.close()
            except Exception:
                pass
    logger.info("Worker finished")


if __name__ == "__main__":
    main()
