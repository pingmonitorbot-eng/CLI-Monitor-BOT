#!/usr/bin/env python3
"""
GitHub Actions worker — monitors all panels, posts to Telegram.

- Panels WITHOUT row time (Blue/Unix): 10-min query window; post whatever is found.
- Panels WITH row time (ITS/Choice/Hadi/Zyron/Lamix): no 10-min row filter;
  save latest event TIME per panel+CLI; only post rows newer than that time
  (same range name allowed if time is newer).
- Lamix: screenshot + up to 3 copy buttons when posting.
- Lamix posts go to lamix_group_chat_id if set.
"""
import os
import sys
import time
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

import requests

from shared_db import (
    get_settings,
    save_settings,
    list_panels,
    ping,
    get_cli_latest_time,
    set_cli_latest_time,
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
    import json
    data = {
        "chat_id": chat_id,
        "caption": caption[:1024],
        "parse_mode": "Markdown",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data=data,
            files={"photo": ("search.png", photo_bytes, "image/png")},
            timeout=60,
        )
        if r.status_code != 200:
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


def format_caption(panel_name: str, cli: str, ranges: List[str]) -> str:
    lines = [
        "🆕 *New Range Found*",
        "━━━━━━━━━━━━━━",
        f"🖥 *Panel:* `{panel_name}`",
        f"🔖 *CLI:* `{cli}`",
        f"📦 *Ranges:* `{len(ranges)}`",
        "",
        "📋 *Ranges:*",
    ]
    for r in ranges[:15]:
        lines.append(f"• `{r}`")
    return "\n".join(lines)


def parse_event_time(raw: str) -> Optional[datetime]:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    if s.endswith("+00"):
        s = s[:-3] + "+0000"
    elif s.endswith("Z"):
        s = s[:-1] + "+0000"
    s2 = s.replace("+0000", "").strip()
    for candidate in (s, s2):
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
        ):
            try:
                dt = datetime.strptime(candidate, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                continue
    return None


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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

        raw_items, df, dt = client.search_cli(
            cli, LOOKBACK_MINUTES, filter_by_time=filter_by_time
        )
        items = []
        for it in raw_items or []:
            rn = (it.get("range_name") or "").strip()
            if not rn:
                continue
            low = rn.lower()
            if low in ("(none)", "none", "-", "range") or "no data" in low:
                continue
            items.append({
                "range_name": rn,
                "event_time": it.get("event_time") or "",
            })
        shot = getattr(client, "_last_screenshot", None)
        logger.info("<< panel=%s OK hits=%s has_row_time=%s", name, len(items), has_row_time)
        return items, shot, has_row_time
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

            a, b, c = result
            if a == "__LOGIN_FAIL__":
                reason = b or "unknown"
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

            items, shot, has_row_time = a, b, c
            if not items:
                continue

            pid = str(panel["_id"])
            post_items = []
            max_dt = None

            if has_row_time:
                prev_iso = get_cli_latest_time(pid, cli)
                prev_dt = parse_event_time(prev_iso) if prev_iso else None

                timed = []
                for it in items:
                    evt = parse_event_time(it.get("event_time") or "")
                    if evt is None:
                        continue
                    timed.append((evt, it))
                    if max_dt is None or evt > max_dt:
                        max_dt = evt

                if not timed:
                    logger.info(
                        "panel=%s cli=%s no parseable event times",
                        panel.get("name"),
                        cli,
                    )
                    continue

                if prev_dt is None:
                    post_items = [it for evt, it in timed if evt == max_dt]
                    logger.info(
                        "panel=%s cli=%s first-run latest @ %s count=%s",
                        panel.get("name"),
                        cli,
                        to_iso(max_dt),
                        len(post_items),
                    )
                else:
                    post_items = [it for evt, it in timed if evt > prev_dt]
                    if not post_items:
                        logger.info(
                            "panel=%s cli=%s no rows after %s (max=%s)",
                            panel.get("name"),
                            cli,
                            prev_iso,
                            to_iso(max_dt) if max_dt else "-",
                        )
                        continue
            else:
                post_items = items

            ranges = [it["range_name"] for it in post_items]
            seen = set()
            ordered = []
            for r in ranges:
                if r not in seen:
                    seen.add(r)
                    ordered.append(r)
            ranges = ordered
            if not ranges:
                continue

            if has_row_time:
                posted_times = [
                    parse_event_time(it.get("event_time") or "")
                    for it in post_items
                ]
                posted_times = [x for x in posted_times if x is not None]
                if posted_times:
                    set_cli_latest_time(pid, cli, to_iso(max(posted_times)))

            caption = format_caption(panel.get("name"), cli, ranges)
            kb = build_copy_keyboard(ranges, limit=3)
            photo = shot if isinstance(shot, (bytes, bytearray)) else None

            ptype = panel.get("type")
            target = group_id
            if ptype == "lamix_sms":
                target = st.get("lamix_group_chat_id") or group_id
            if target:
                if photo:
                    ok = tg_send_photo(int(target), bytes(photo), caption, reply_markup=kb)
                    if not ok:
                        tg_send(int(target), caption, reply_markup=kb)
                else:
                    tg_send(int(target), caption, reply_markup=kb)

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
