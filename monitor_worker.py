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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
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
PANEL_TIMEOUT = int(os.getenv("PANEL_TIMEOUT", "90"))  # seconds per panel search


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
        rows.append([{"text": f"📋 {label}", "copy_text": {"text": name}}])
    return {"inline_keyboard": rows}


def heartbeat():
    save_settings(
        worker_heartbeat=datetime.now(timezone.utc).isoformat(),
        worker_run_id=GITHUB_RUN_ID or "",
    )


def search_one_panel(panel: dict, cli: str, client_cache: dict):
    """Run login+search for one panel. Returns (ranges, df, dt) or None."""
    pid = str(panel["_id"])
    name = panel.get("name") or pid
    ptype = panel.get("type")
    logger.info(">> panel=%s type=%s cli=%s", name, ptype, cli)
    try:
        client = client_cache.get(pid)
        if client is None:
            client = make_client(panel)
            client_cache[pid] = client
        if not client.ensure_login():
            reason = getattr(client, "last_error", "") or "unknown"
            logger.error("<< panel=%s LOGIN FAIL | %s", name, reason)
            client_cache.pop(pid, None)
            if hasattr(client, "close"):
                try:
                    client.close()
                except Exception:
                    pass
            # Return special marker so caller can notify Telegram
            return ("__LOGIN_FAIL__", reason, "")
        items, df, dt = client.search_cli(cli, LOOKBACK_MINUTES)
        ranges = [it.get("range_name") or "?" for it in items] if items else []
        logger.info("<< panel=%s OK hits=%s", name, len(ranges))
        return ranges, df, dt
    except Exception as e:
        logger.error("<< panel=%s ERROR: %s", name, e)
        logger.debug(traceback.format_exc())
        client_cache.pop(pid, None)
        return None


def main():
    logger.info("Worker starting max_hours=%.2f panel_timeout=%ss", WORKER_MAX_HOURS, PANEL_TIMEOUT)
    try:
        ping()
        logger.info("Mongo OK")
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

    try:
        _st = get_settings()
        _gid = _st.get("group_chat_id")
        if _gid:
            tg_send(
                int(_gid),
                (
                    "✅ *Worker deployed successfully*\n"
                    f"Run: `{GITHUB_RUN_ID or 'local'}`\n"
                    f"Max hours: `{WORKER_MAX_HOURS}`"
                ),
            )
    except Exception as e:
        logger.error("startup notify: %s", e)

    client_cache = {}
    login_fail_notified = set()
    cursor = 0
    cycle_idx = 0
    pool = ThreadPoolExecutor(max_workers=1)

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

        if cursor == 0:
            logger.info(
                "Cycle panels=%s types=%s clis=%s",
                len(panels),
                [p.get("type") for p in panels],
                len(clis),
            )

        if cursor >= len(clis):
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
        logger.info("=== CLI=%s (%s/%s) ===", cli, cursor, len(clis))

        for panel in panels:
            fut = pool.submit(search_one_panel, panel, cli, client_cache)
            try:
                result = fut.result(timeout=PANEL_TIMEOUT)
            except FuturesTimeout:
                logger.error(
                    "panel=%s TIMEOUT after %ss (type=%s)",
                    panel.get("name"),
                    PANEL_TIMEOUT,
                    panel.get("type"),
                )
                # drop possible hung browser client
                pid = str(panel["_id"])
                old = client_cache.pop(pid, None)
                if old and hasattr(old, "close"):
                    try:
                        old.close()
                    except Exception:
                        pass
                continue
            except Exception as e:
                logger.error("panel future error: %s", e)
                continue

            if not result:
                continue
            ranges, df, dt = result
            if ranges == "__LOGIN_FAIL__":
                reason = df or "unknown"
                if group_id:
                    pid = str(panel.get("_id"))
                    if pid not in login_fail_notified:
                        login_fail_notified.add(pid)
                        fail_msg = (
                            "⚠️ *Login failed*\n"
                            f"Panel: `{panel.get('name')}`\n"
                            f"Type: `{panel.get('type')}`\n"
                            f"URL: `{panel.get('base_url') or '-'}`\n"
                            f"Reason:\n`{str(reason)[:500]}`"
                        )
                        tg_send(int(group_id), fail_msg)
                continue
            if not ranges:
                continue
            # drop junk labels (Lamix empty table leftovers)
            ranges = [
                r for r in ranges
                if r and r.strip() and r.strip().lower() not in ("(none)", "none", "-", "range")
                and "no data" not in r.lower()
            ]
            if not ranges:
                continue
            msg = format_alert(panel.get("name"), cli, ranges, df, dt)
            kb = build_copy_keyboard(ranges)
            if group_id:
                tg_send(int(group_id), msg, reply_markup=kb)

        time.sleep(CHECK_INTERVAL)

    for c in list(client_cache.values()):
        if hasattr(c, "close"):
            try:
                c.close()
            except Exception:
                pass
    pool.shutdown(wait=False)
    logger.info("Worker finished")


if __name__ == "__main__":
    main()
