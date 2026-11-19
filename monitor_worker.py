#!/usr/bin/env python3
"""
GitHub Actions worker — monitors all panels, posts to Telegram.
- Panels without row timestamps (Blue/Unix): 10-min query window
- Panels with timestamps: no time filter; DB latest-range dedup
- Lamix: screenshot on new ranges + up to 3 copy buttons
"""
import os
import sys
import time
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from typing import List, Optional

import requests

from shared_db import (
    get_settings,
    save_settings,
    list_panels,
    ping,
    get_cli_state,
    set_cli_state,
)
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
PANEL_TIMEOUT = int(os.getenv("PANEL_TIMEOUT", "120"))


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


def tg_send_photo(chat_id: int, photo_bytes: bytes, caption: str, reply_markup: dict = None):
    if not BOT_TOKEN or not chat_id or not photo_bytes:
        return False
    data = {
        "chat_id": chat_id,
        "caption": caption[:1024],
        "parse_mode": "Markdown",
    }
    if reply_markup:
        import json
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data=data,
            files={"photo": ("search.png", photo_bytes, "image/png")},
            timeout=60,
        )
        if r.status_code != 200:
            # retry without markdown
            data.pop("parse_mode", None)
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data=data,
                files={"photo": ("search.png", photo_bytes, "image/png")},
                timeout=60,
            )
        return r.status_code == 200
    except Exception as e:
        logger.error("tg_send_photo: %s", e)
        return False


def build_copy_keyboard(ranges: List[str], limit: int = 3) -> Optional[dict]:
    if not ranges:
        return None
    rows = []
    for name in ranges[:limit]:
        label = name if len(name) <= 40 else name[:37] + "..."
        rows.append([{"text": f"📋 {label}", "copy_text": {"text": name}}])
    return {"inline_keyboard": rows} if rows else None


def format_caption(panel_name: str, cli: str, ranges: List[str], new_ranges: List[str]) -> str:
    show = new_ranges or ranges
    lines = [
        "🆕 *New Range Found*",
        "━━━━━━━━━━━━━━",
        f"🖥 *Panel:* `{panel_name}`",
        f"🔖 *CLI:* `{cli}`",
        f"📦 *New:* `{len(new_ranges)}` | Total: `{len(ranges)}`",
        "",
        "📋 *Ranges:*",
    ]
    for r in show[:15]:
        lines.append(f"• `{r}`")
    return "\n".join(lines)


def heartbeat():
    save_settings(
        worker_heartbeat=datetime.now(timezone.utc).isoformat(),
        worker_run_id=GITHUB_RUN_ID or "",
    )


def search_one_panel(panel: dict, cli: str, client_cache: dict):
    pid = str(panel["_id"])
    name = panel.get("name") or pid
    ptype = panel.get("type")
    info = PANEL_TYPES.get(ptype) or {}
    has_row_time = bool(info.get("has_row_time"))
    # Blue/Unix: filter_by_time True (query window). Dated panels: False
    filter_by_time = not has_row_time
    logger.info(
        ">> panel=%s type=%s cli=%s filter_time=%s",
        name, ptype, cli, filter_by_time,
    )
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
            return ("__LOGIN_FAIL__", reason, None)
        items, df, dt = client.search_cli(
            cli, LOOKBACK_MINUTES, filter_by_time=filter_by_time
        )
        ranges = []
        for it in items or []:
            rn = (it.get("range_name") or "").strip()
            if not rn:
                continue
            low = rn.lower()
            if low in ("(none)", "none", "-", "range") or "no data" in low:
                continue
            ranges.append(rn)
        # de-dupe preserve order
        seen = set()
        ordered = []
        for r in ranges:
            if r not in seen:
                seen.add(r)
                ordered.append(r)
        ranges = ordered
        shot = getattr(client, "_last_screenshot", None)
        logger.info("<< panel=%s OK hits=%s", name, len(ranges))
        return ranges, shot, df
    except Exception as e:
        logger.error("<< panel=%s ERROR: %s", name, e)
        logger.debug(traceback.format_exc())
        client_cache.pop(pid, None)
        return None


def main():
    logger.info(
        "Worker starting max_hours=%.2f panel_timeout=%ss",
        WORKER_MAX_HOURS,
        PANEL_TIMEOUT,
    )
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
                    "panel=%s TIMEOUT after %ss",
                    panel.get("name"),
                    PANEL_TIMEOUT,
                )
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

            ranges, shot_or_reason, _df = result
            if ranges == "__LOGIN_FAIL__":
                reason = shot_or_reason or "unknown"
                if group_id:
                    pid = str(panel.get("_id"))
                    if pid not in login_fail_notified:
                        login_fail_notified.add(pid)
                        tg_send(
                            int(group_id),
                            (
                                "⚠️ *Login failed*\n"
                                f"Panel: `{panel.get('name')}`\n"
                                f"Type: `{panel.get('type')}`\n"
                                f"URL: `{panel.get('base_url') or '-'}`\n"
                                f"Reason:\n`{str(reason)[:500]}`"
                            ),
                        )
                continue

            if not ranges:
                continue

            pid = str(panel["_id"])
            prev = get_cli_state(pid, cli)
            prev_set = set(prev)
            new_ranges = [r for r in ranges if r not in prev_set]

            # Same latest set → skip post
            if not new_ranges and set(ranges) == prev_set and prev:
                logger.info(
                    "panel=%s cli=%s no new ranges (saved=%s)",
                    panel.get("name"),
                    cli,
                    len(prev),
                )
                continue
            if not new_ranges and prev:
                # identical content different order
                if set(ranges) == prev_set:
                    continue

            # First ever sighting with empty prev: treat all as new
            if not prev:
                new_ranges = list(ranges)

            if not new_ranges:
                continue

            # Save full current latest snapshot
            set_cli_state(pid, cli, ranges)

            caption = format_caption(panel.get("name"), cli, ranges, new_ranges)
            # Copy buttons: latest 3 of current result list (API usually newest-first)
            kb = build_copy_keyboard(ranges, limit=3)
            shot = shot_or_reason if isinstance(shot_or_reason, (bytes, bytearray)) else None

            if group_id:
                if shot:
                    ok = tg_send_photo(int(group_id), bytes(shot), caption, reply_markup=kb)
                    if not ok:
                        tg_send(int(group_id), caption, reply_markup=kb)
                else:
                    tg_send(int(group_id), caption, reply_markup=kb)

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
