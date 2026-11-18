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
    """Playwright-based Lamix panel (URL provided per panel)."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def login(self) -> bool:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("Playwright not installed")
            return False
        try:
            if self._page is None:
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--single-process"],
                )
                self._context = self._browser.new_context()
                self._page = self._context.new_page()
            page = self._page
            login_url = self.base_url
            if not login_url.endswith("/login"):
                # try common paths
                login_url = self.base_url.rstrip("/") + "/login"
            page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1000)
            # fill username/password flexibly
            user_sel = 'input[type="text"], input[type="email"], input[name*="user" i], input[name*="email" i], input[placeholder*="user" i], input[placeholder*="email" i]'
            pass_sel = 'input[type="password"]'
            page.locator(user_sel).first.fill(self.username, timeout=15000)
            page.locator(pass_sel).first.fill(self.password, timeout=15000)
            btn = page.locator('button[type="submit"], button:has-text("Login"), button:has-text("Sign in"), input[type="submit"]').first
            btn.click(timeout=15000)
            page.wait_for_timeout(3000)
            # consider login ok if URL left login page or dashboard markers
            if "login" in page.url.lower():
                # maybe still loading
                page.wait_for_timeout(3000)
            ok = "login" not in page.url.lower() or page.locator("text=/dashboard|search|cli/i").count() > 0
            if ok:
                logger.info("Lamix login OK (%s)", self.base_url)
            else:
                logger.error("Lamix login may have failed url=%s", page.url)
            return ok
        except Exception as e:
            logger.error("Lamix login error: %s", e)
            return False

    def ensure_login(self) -> bool:
        if self._page is not None:
            try:
                self._page.evaluate("1")
                return True
            except Exception:
                self._page = None
        return self.login()

    def search_cli(self, cli: str, minutes: int = LOOKBACK_MINUTES) -> tuple:
        now = datetime.now(timezone.utc)
        df = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        dt = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if not self.ensure_login():
            return [], df, dt
        page = self._page
        items = []
        try:
            # Navigate to common search paths
            candidates = [
                self.base_url.rstrip("/") + "/cli-search",
                self.base_url.rstrip("/") + "/dashboard/cli-search",
                self.base_url.rstrip("/") + "/search",
                self.base_url,
            ]
            for url in candidates:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(800)
                    if page.locator("input").count() > 0:
                        break
                except Exception:
                    continue
            # try fill search box
            search_inputs = page.locator('input[type="text"], input[type="search"], input:not([type="password"])')
            filled = False
            for i in range(min(search_inputs.count(), 6)):
                el = search_inputs.nth(i)
                try:
                    ph = (el.get_attribute("placeholder") or "").lower()
                    name = (el.get_attribute("name") or "").lower()
                    if any(k in ph or k in name for k in ("cli", "search", "sender", "query")) or i == 0:
                        el.fill(cli, timeout=5000)
                        filled = True
                        break
                except Exception:
                    continue
            if not filled and search_inputs.count() > 0:
                search_inputs.first.fill(cli, timeout=5000)
            # click search
            for sel in ['button:has-text("Search")', 'button[type="submit"]', 'button:has-text("সার্চ")']:
                loc = page.locator(sel)
                if loc.count():
                    loc.first.click(timeout=8000)
                    break
            page.wait_for_timeout(2500)
            # scrape table rows / cards for range-like text
            rows = page.locator("table tbody tr, .range-item, .search-result, [class*='result']")
            n = min(rows.count(), 30)
            for i in range(n):
                try:
                    txt = rows.nth(i).inner_text(timeout=2000).strip()
                    if not txt or len(txt) < 2:
                        continue
                    # first line or full cell as range name
                    line = txt.splitlines()[0].strip()[:200]
                    items.append({"range_name": line, "range_id": f"{cli}:{line}"})
                except Exception:
                    continue
            # fallback: any td with pipe-separated names
            if not items:
                tds = page.locator("td")
                for i in range(min(tds.count(), 40)):
                    try:
                        t = tds.nth(i).inner_text(timeout=1000).strip()
                        if t and len(t) > 3 and t.lower() not in ("date", "range", "cli"):
                            items.append({"range_name": t[:200], "range_id": f"{cli}:{t[:80]}"})
                    except Exception:
                        continue
        except Exception as e:
            logger.error("Lamix search error: %s", e)
        return items, df, dt

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._page = None
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
