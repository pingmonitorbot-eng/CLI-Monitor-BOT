"""Panel clients — used by Worker (and Controller test search)."""
import os
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple

import requests

logger = logging.getLogger("panels")

LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "10"))

PANEL_TYPES = {
    "blue_sms": {
        "label": "Blue SMS",
        "api_base": "https://api.blue-sms.net/api/v1",
    },
    "unix_sms": {
        "label": "Unix SMS",
        "api_base": "https://api.unixsms.com/api/v1",
    },
    "its_sms": {
        "label": "ITS SMS",
        "base_url": "http://51.77.132.62/ints",
    },
    "choice_sms": {
        "label": "Choice SMS",
        "base_url": "http://51.77.52.79/ints",
        "search_mode": "access",  # AccessSearch / data_accesssearch.php
    },
    "hadi_sms": {
        "label": "SMS Hadi",
        "base_url": "http://2.59.169.96/ints",
        "search_mode": "cli",  # CLISearch / data_clisearch.php
    },
    "zyron_sms": {
        "label": "Zyron SMS",
        "base_url": "http://151.80.19.204/ints",
        "search_mode": "cli",
    },
    "lamix_sms": {
        "label": "Lamix SMS",
        "needs_url": True,  # user must provide panel base URL
        "needs_browser": True,
    },
}


class GenericSMSClient:
    """Shared client for Blue SMS / Unix SMS (same API shape)."""

    def __init__(self, api_base: str, username: str, password: str):
        self.api_base = api_base.rstrip("/")
        self.username = username
        self.password = password
        self.access_token: Optional[str] = None
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; SMS-Monitor/2.1)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def login(self) -> bool:
        try:
            r = self.session.post(
                f"{self.api_base}/auth/login",
                json={"identifier": self.username, "password": self.password},
                timeout=20,
            )
            data = r.json()
            if r.status_code == 200 and data.get("success"):
                self.access_token = data["data"]["access_token"]
                self.session.headers["Authorization"] = f"Bearer {self.access_token}"
                return True
            logger.error(f"Login fail ({self.api_base}): {data}")
            return False
        except Exception as e:
            logger.error(f"Login error ({self.api_base}): {e}")
            return False

    def ensure_login(self) -> bool:
        if self.access_token:
            try:
                r = self.session.get(f"{self.api_base}/auth/me", timeout=12)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
        return self.login()

    def search_cli(self, cli: str, minutes: int = LOOKBACK_MINUTES) -> tuple:
        now = datetime.now(timezone.utc)
        date_to = now
        date_from = now - timedelta(minutes=minutes)
        df = date_from.strftime("%Y-%m-%dT%H:%M:%SZ")
        dt = date_to.strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "cli": cli,
            "date_from": df,
            "date_to": dt,
            "page": 1,
            "page_size": 50,
        }
        try:
            r = self.session.get(
                f"{self.api_base}/my/sms/range-message-search",
                params=params,
                timeout=25,
            )
            data = r.json()
            if r.status_code == 200 and data.get("success"):
                return data.get("data", {}).get("items", []), df, dt
            logger.warning(f"Search fail {cli}: {data}")
            return [], df, dt
        except Exception as e:
            logger.error(f"Search error {cli}: {e}")
            return [], df, dt


class ITSSMSClient:
    """Legacy INTS-style panel (ITS CLISearch or Choice AccessSearch)."""

    def __init__(self, base_url: str, username: str, password: str, search_mode: str = "cli"):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.search_mode = search_mode  # "cli" | "access"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self.logged_in = False

    def login(self) -> bool:
        try:
            r = self.session.get(f"{self.base_url}/login", timeout=20)
            r.raise_for_status()
            import re
            m = re.search(r"What is\s+(\d+)\s*\+\s*(\d+)", r.text, re.I)
            if not m:
                logger.error("ITS captcha not found")
                return False
            answer = str(int(m.group(1)) + int(m.group(2)))
            r2 = self.session.post(
                f"{self.base_url}/signin",
                data={
                    "username": self.username,
                    "password": self.password,
                    "capt": answer,
                },
                timeout=20,
                allow_redirects=True,
            )
            # success if redirected away from login or dashboard content
            ok = "login" not in r2.url.lower() or "Dashboard" in r2.text or "CLI Search" in r2.text
            if not ok and r2.status_code == 200 and "SMSDashboard" in r2.url:
                ok = True
            # also try visiting dashboard
            if not ok:
                r3 = self.session.get(f"{self.base_url}/agent/SMSDashboard", timeout=15)
                ok = r3.status_code == 200 and ("Dashboard" in r3.text or "tech_" in r3.text or "CLI" in r3.text)
            self.logged_in = ok
            if ok:
                logger.info("ITS login OK")
            else:
                logger.error(f"ITS login failed url={r2.url}")
            return ok
        except Exception as e:
            logger.error(f"ITS login error: {e}")
            return False

    def ensure_login(self) -> bool:
        if self.logged_in:
            try:
                page = "AccessSearch" if self.search_mode == "access" else "CLISearch"
                r = self.session.get(f"{self.base_url}/agent/{page}", timeout=12)
                if r.status_code == 200 and ("Search" in r.text or "Dashboard" in r.text):
                    return True
            except Exception:
                pass
        return self.login()

    def search_cli(self, cli: str, minutes: int = LOOKBACK_MINUTES) -> tuple:
        """
        Returns (items, df, dt)
        items: list of dicts with range_name, event_time
        Only rows whose Date is within last `minutes`.
        Searched Time in posts uses those panel dates.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=minutes)

        if self.search_mode == "access":
            # Choice SMS AccessSearch — no custom datetime; use Today (1)
            endpoint = f"{self.base_url}/agent/res/data_accesssearch.php"
            referer = f"{self.base_url}/agent/AccessSearch"
            params = {
                "fcli": cli,
                "fcontent": "",
                "ftimedur": "1",  # Today
                "ffm": "1",
                "sEcho": "1",
                "iColumns": "5",
                "iDisplayStart": "0",
                "iDisplayLength": "50",
                "mDataProp_0": "0",
                "mDataProp_1": "1",
                "mDataProp_2": "2",
                "mDataProp_3": "3",
                "mDataProp_4": "4",
                "sSearch": "",
                "bRegex": "false",
                "iSortCol_0": "0",
                "sSortDir_0": "desc",
                "iSortingCols": "1",
            }
            date_idx, range_idx = 0, 1
        else:
            endpoint = f"{self.base_url}/agent/res/data_clisearch.php"
            referer = f"{self.base_url}/agent/CLISearch"
            q_from = (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
            q_to = now.strftime("%Y-%m-%d 23:59:59")
            params = {
                "fdate1": q_from,
                "fdate2": q_to,
                "fcli": cli,
                "fcontent": "",
                "sEcho": "1",
                "iColumns": "2",
                "sColumns": ",",
                "iDisplayStart": "0",
                "iDisplayLength": "50",
                "mDataProp_0": "0",
                "mDataProp_1": "1",
                "sSearch": "",
                "bRegex": "false",
                "iSortCol_0": "0",
                "sSortDir_0": "desc",
                "iSortingCols": "1",
            }
            date_idx, range_idx = 0, 1

        try:
            r = self.session.get(
                endpoint,
                params=params,
                timeout=25,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": referer,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
            )
            data = r.json()
            rows = data.get("aaData") or []
            items = []
            times = []
            for row in rows:
                if not row or len(row) <= max(date_idx, range_idx):
                    continue
                date_str = str(row[date_idx]).strip()
                range_name = str(row[range_idx]).strip()
                try:
                    evt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                except Exception:
                    continue
                if evt < cutoff:
                    continue
                items.append({
                    "range_name": range_name,
                    "event_time": date_str,
                    "range_id": f"{date_str}|{range_name}",
                })
                times.append(date_str)
            if times:
                df, dt = min(times), max(times)
            else:
                df = cutoff.strftime("%Y-%m-%d %H:%M:%S")
                dt = now.strftime("%Y-%m-%d %H:%M:%S")
            return items, df, dt
        except Exception as e:
            logger.error(f"INTS search error ({self.search_mode}): {e}")
            df = cutoff.strftime("%Y-%m-%d %H:%M:%S")
            dt = now.strftime("%Y-%m-%d %H:%M:%S")
            return [], df, dt


class LamixSMSClient:
    """Playwright UI client for Lamix (API is signature-protected)."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def _ensure_browser(self):
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self._page = self._context.new_page()
        self._page.set_default_timeout(45000)

    def _fill_react_input(self, locator, value: str):
        locator.click(timeout=15000)
        locator.fill("")
        locator.type(value, delay=20)
        locator.evaluate(
            """(el, v) => {
                const proto = window.HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                desc.set.call(el, v);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )

    def login(self) -> bool:
        try:
            self._ensure_browser()
        except Exception as e:
            logger.error("Playwright init failed: %s", e)
            return False
        page = self._page
        try:
            login_url = self.base_url if self.base_url.endswith("/login") else self.base_url + "/login"
            page.goto(login_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(800)

            # Username / password fields
            user = page.locator('input[placeholder*="User" i], input[type="text"], input[type="email"]').first
            pwd = page.locator('input[type="password"]').first
            user.wait_for(state="visible", timeout=20000)
            self._fill_react_input(user, self.username)
            self._fill_react_input(pwd, self.password)
            page.wait_for_timeout(300)

            btn = page.locator('button:has-text("LOGIN"), button:has-text("Login"), button[type="submit"]').first
            btn.click()
            # wait for navigation away from login
            page.wait_for_timeout(2000)
            try:
                page.wait_for_url(lambda u: "login" not in u.lower(), timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            url = page.url.lower()
            body = page.content()
            ok = ("login" not in url) or ("dashboard" in url) or ("cli-search" in url) or ("Welcome" in body)
            # stronger check: /api/me via page context
            try:
                me = page.evaluate("""async () => {
                    const r = await fetch('/api/me', {credentials:'include'});
                    return {status: r.status, text: await r.text()};
                }""")
                if me and me.get("status") == 200 and "username" in (me.get("text") or ""):
                    ok = True
            except Exception:
                pass

            if ok:
                logger.info("Lamix login OK (%s) url=%s", self.base_url, page.url)
            else:
                logger.error("Lamix login failed url=%s", page.url)
            return ok
        except Exception as e:
            logger.error("Lamix login error: %s", e)
            return False

    def ensure_login(self) -> bool:
        if self._page is not None:
            try:
                me = self._page.evaluate("""async () => {
                    const r = await fetch('/api/me', {credentials:'include'});
                    return r.status;
                }""")
                if me == 200:
                    return True
            except Exception:
                pass
            try:
                self.close()
            except Exception:
                pass
            self._page = None
        return self.login()

    def search_cli(self, cli: str, minutes: int = LOOKBACK_MINUTES) -> tuple:
        now = datetime.now(timezone.utc)
        df = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        dt = now.strftime("%Y-%m-%d %H:%M:%S")
        if not self.ensure_login():
            return [], df, dt

        page = self._page
        items = []
        times = []
        try:
            search_url = self.base_url.rstrip("/") + "/cli-search"
            if self.base_url.endswith("/login"):
                search_url = self.base_url.replace("/login", "") + "/cli-search"
            page.goto(search_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1000)

            # Sender (CLI) input
            cli_input = page.locator(
                'input[placeholder*="sender" i], input[placeholder*="facebook" i], '
                'input[placeholder*="CLI" i], input[placeholder*="full sender" i]'
            ).first
            if cli_input.count() == 0:
                cli_input = page.locator("input[type='text']").first
            cli_input.wait_for(state="visible", timeout=15000)
            self._fill_react_input(cli_input, cli)
            page.wait_for_timeout(200)

            search_btn = page.locator('button:has-text("Search"), button:has-text("SEARCH")').first
            search_btn.click()
            page.wait_for_timeout(3000)

            # Parse table: Range | Last seen
            rows = page.locator("table tbody tr")
            n = rows.count()
            cutoff = now - timedelta(minutes=minutes)
            for i in range(n):
                try:
                    cells = rows.nth(i).locator("td")
                    if cells.count() < 1:
                        continue
                    range_name = cells.nth(0).inner_text(timeout=2000).strip()
                    last_seen = cells.nth(1).inner_text(timeout=2000).strip() if cells.count() > 1 else ""
                    if not range_name or range_name.lower().startswith("no data"):
                        continue
                    # parse last seen — e.g. 2026-11-18 17:20:56.288+00
                    evt = None
                    for fmt in (
                        "%Y-%m-%d %H:%M:%S.%f%z",
                        "%Y-%m-%d %H:%M:%S%z",
                        "%Y-%m-%d %H:%M:%S.%f",
                        "%Y-%m-%d %H:%M:%S",
                    ):
                        try:
                            raw = last_seen.replace("+00", "+0000") if last_seen.endswith("+00") else last_seen
                            evt = datetime.strptime(raw, fmt)
                            if evt.tzinfo is None:
                                evt = evt.replace(tzinfo=timezone.utc)
                            break
                        except Exception:
                            continue
                    if evt is not None and evt < cutoff:
                        continue
                    items.append({
                        "range_name": range_name,
                        "event_time": last_seen or dt,
                        "range_id": f"{cli}:{range_name}:{last_seen}",
                    })
                    if last_seen:
                        times.append(last_seen.split(".")[0] if "." in last_seen else last_seen)
                except Exception:
                    continue

            if times:
                df, dt = min(times), max(times)
            logger.info("Lamix search cli=%s hits=%s", cli, len(items))
        except Exception as e:
            logger.error("Lamix search error: %s", e)
        return items, df, dt

    def close(self):
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None



def make_client(panel: dict):
    ptype = panel.get("type", "blue_sms")
    info = PANEL_TYPES.get(ptype)
    if not info:
        raise ValueError(f"Unknown panel type: {ptype}")
    if ptype == "lamix_sms":
        base = panel.get("base_url") or panel.get("url") or ""
        if not base:
            raise ValueError("Lamix panel requires base_url")
        return LamixSMSClient(base, panel["username"], panel["password"])
    if ptype in ("its_sms", "choice_sms", "hadi_sms", "zyron_sms"):
        mode = info.get("search_mode", "cli")
        return ITSSMSClient(
            info["base_url"], panel["username"], panel["password"], search_mode=mode
        )
    return GenericSMSClient(info["api_base"], panel["username"], panel["password"])
