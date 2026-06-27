import os
import json
import time
import logging
import random
import socket
import subprocess
import threading
import urllib.request
import platform

import config

logger = logging.getLogger("StreamSaver.CookieManager")

IS_WINDOWS = platform.system() == "Windows"
EDGE_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-extensions",
    "--disable-gpu",
]
LOCK_FILE = os.path.join(config.BASE_DIR, "bot_edge.lock")


def _pick_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_lock():
    if not os.path.exists(LOCK_FILE):
        return None, None
    try:
        with open(LOCK_FILE) as f:
            data = json.load(f)
        return data.get("pid"), data.get("port")
    except Exception:
        return None, None


def _write_lock(pid, port):
    try:
        with open(LOCK_FILE, "w") as f:
            json.dump({"pid": pid, "port": port}, f)
    except Exception as e:
        logger.warning("Failed to write lock: %s", e)


def _clear_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


class CookieManager:
    def __init__(self):
        self.cookie_valid = False
        self.edge_pid = None
        self.cdp_port = None
        self._lock = threading.Lock()
        self._on_status_change = None
        self._running = False

    def on_status_change(self, callback):
        self._on_status_change = callback

    def _kill_edge(self):
        pid = self.edge_pid
        self.edge_pid = None
        if pid and IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/pid", str(pid), "/f"],
                    capture_output=True, text=True, timeout=5)
                time.sleep(0.5)
            except Exception as e:
                logger.warning("Failed to kill bot Edge (PID %d): %s", pid, e)

    def _is_running(self, pid):
        if not pid:
            return False
        try:
            if IS_WINDOWS:
                r = subprocess.run(
                    ["tasklist", "/fi", f"PID eq {pid}"],
                    capture_output=True, text=True, timeout=5)
                return str(pid) in r.stdout
            else:
                os.kill(pid, 0)
                return True
        except Exception:
            return False

    def _cleanup_stale(self):
        pid, port = _read_lock()
        if pid and self._is_running(pid) and port:
            self.edge_pid = pid
            self.cdp_port = port
            logger.info("Reusing existing Edge (PID: %d, port: %d)", pid, port)
            return True
        if pid:
            self._kill_pid(pid)
        _clear_lock()
        return False

    def _kill_pid(self, pid):
        if not pid or not IS_WINDOWS:
            return
        try:
            subprocess.run(["taskkill", "/pid", str(pid), "/f"],
                          capture_output=True, timeout=5)
        except Exception:
            pass

    def start_headless(self):
        if not IS_WINDOWS:
            return False
        if not os.path.exists(config.EDGE_PATH):
            logger.error("Edge not found at %s", config.EDGE_PATH)
            return False

        if self._cleanup_stale():
            return True

        port = _pick_port()
        try:
            profile = config.BOT_EDGE_PROFILE
            os.makedirs(profile, exist_ok=True)
            cmd = [
                config.EDGE_PATH,
                f"--user-data-dir={profile}",
                "--headless=new",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                *EDGE_FLAGS,
                "--disable-features=msAppBoundEncryption",
                "about:blank",
            ]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.edge_pid = proc.pid
            self.cdp_port = port
            _write_lock(proc.pid, port)
            logger.info("Edge headless started (PID: %d, port: %d)", proc.pid, port)
            time.sleep(2)
            return True
        except Exception as e:
            logger.error("Failed to start Edge: %s", e)
            self.edge_pid = None
            self.cdp_port = None
            _clear_lock()
            return False

    def start_visible(self):
        if not IS_WINDOWS:
            return False
        self._kill_edge()
        port = _pick_port()
        try:
            profile = config.BOT_EDGE_PROFILE
            os.makedirs(profile, exist_ok=True)
            cmd = [
                config.EDGE_PATH,
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                *EDGE_FLAGS,
                "--disable-features=msAppBoundEncryption",
                "https://accounts.google.com/ServiceLogin?service=youtube",
            ]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.edge_pid = proc.pid
            self.cdp_port = port
            _write_lock(proc.pid, port)
            logger.info("Edge visible started for login (PID: %d, port: %d)", proc.pid, port)
            time.sleep(3)
            return True
        except Exception as e:
            logger.error("Failed to start visible Edge: %s", e)
            self.edge_pid = None
            self.cdp_port = None
            _clear_lock()
            return False

    def _cdp_request(self, path):
        if not self.cdp_port:
            return None
        try:
            url = f"http://127.0.0.1:{self.cdp_port}{path}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.debug("CDP %s failed: %s", path, e)
            return None

    def _cdp_ws_url(self):
        data = self._cdp_request("/json/version")
        if data:
            return data.get("webSocketDebuggerUrl")
        return None

    def _cdp_get_all_cookies(self):
        ws_url = None
        for attempt in range(3):
            ws_url = self._cdp_ws_url()
            if ws_url:
                break
            time.sleep(1.5 * (attempt + 1))
        if not ws_url:
            logger.warning("CDP WebSocket URL not available after retries")
            return None

        import websocket
        for origin in (None, f"http://127.0.0.1:{self.cdp_port}"):
            try:
                kw = {"timeout": 10}
                if origin is None:
                    kw["origin"] = None
                else:
                    kw["origin"] = origin
                ws = websocket.create_connection(ws_url, **kw)
                ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
                resp = ws.recv()
                ws.close()
                data = json.loads(resp)
                return data.get("result", {}).get("cookies", [])
            except Exception as e:
                if "403" in str(e) and origin is None:
                    logger.debug("CDP 403 with origin=None, retrying with explicit origin")
                    continue
                logger.error("CDP getAllCookies failed: %s", e)
                return None
        return None

    def _cookies_to_netscape(self, cookies):
        lines = [
            "# Netscape HTTP Cookie File",
            "# https://curl.haxx.se/rfc/cookie_spec.html",
            "# Generated by StreamSaver CDP",
        ]
        for c in cookies:
            domain = c.get("domain", "")
            if not domain:
                continue
            if not domain.startswith("."):
                domain = "." + domain
            path_val = c.get("path", "/")
            secure = "TRUE" if c.get("secure") else "FALSE"
            expires = int(c.get("expires", 0)) or 0
            name = c.get("name", "")
            value = c.get("value", "")
            lines.append(
                f"{domain}\tTRUE\t{path_val}\t{secure}\t{expires}\t{name}\t{value}")
        return "\n".join(lines) + "\n"

    def extract_cookies(self):
        with self._lock:
            if not self._is_running(self.edge_pid):
                logger.warning("Edge not running, cannot extract cookies")
                return False

            cookies = self._cdp_get_all_cookies()
            if not cookies:
                logger.warning("No cookies from CDP")
                self.cookie_valid = False
                return False

            yt_cookies = [
                c for c in cookies
                if "youtube.com" in c.get("domain", "")
                or ".youtube" in c.get("domain", "")
                or "google.com" in c.get("domain", "")
            ]
            target = yt_cookies if yt_cookies else cookies
            netscape = self._cookies_to_netscape(target)
            with open(config.COOKIE_FILE, "w", encoding="utf-8") as f:
                f.write(netscape)

            sz = os.path.getsize(config.COOKIE_FILE)
            self.cookie_valid = sz > 200
            logger.info(
                "Cookies saved: %d total, %d YouTube, %d bytes",
                len(cookies), len(yt_cookies), sz)
            return self.cookie_valid

    def refresh_cookies(self):
        logger.info("Cookie refresh cycle...")
        success = self.extract_cookies()
        if success:
            logger.info("Cookie refresh OK")
        else:
            logger.warning("Cookie refresh failed, retry in 30min")
        if self._on_status_change:
            self._on_status_change("cookie_refresh", success)
        return success

    def start_auto_refresh(self):
        self._running = True

        def _loop():
            while self._running:
                time.sleep(config.COOKIE_REFRESH_INTERVAL)
                if self._running:
                    self.refresh_cookies()

        thread = threading.Thread(target=_loop, daemon=True)
        thread.start()
        logger.info("Auto cookie refresh every %ds", config.COOKIE_REFRESH_INTERVAL)

    def login_flow(self, on_done=None):
        if not IS_WINDOWS:
            if on_done:
                on_done(False)
            return

        def _wait_for_login():
            self._kill_edge()
            self.start_visible()
            if not self.edge_pid:
                if on_done:
                    on_done(False)
                return

            start = time.time()
            timeout = 300
            logged_in = False
            while time.time() - start < timeout:
                if not self._is_running(self.edge_pid):
                    logger.info("Edge closed by user during login")
                    break
                cookies = self._cdp_get_all_cookies()
                if cookies:
                    session_keys = {"LOGIN_INFO", "SAPISID", "APISID", "__Secure-3PSID"}
                    has_session = any(
                        c.get("name") in session_keys and c.get("value")
                        for c in cookies
                    )
                    if has_session:
                        logged_in = True
                        break
                time.sleep(2)

            if logged_in:
                logger.info("YouTube login detected via CDP")
            else:
                logger.info("Login timeout or Edge closed without login session")

            self.extract_cookies()
            self.start_headless()
            success = self.cookie_valid
            if success:
                logger.info("Login successful after headless restart")
            else:
                logger.warning("Login may have failed")
            if on_done:
                on_done(success)

        thread = threading.Thread(target=_wait_for_login, daemon=True)
        thread.start()

    def stop(self):
        self._running = False
        self._kill_edge()
        _clear_lock()

    def get_status(self):
        f_exists = os.path.exists(config.COOKIE_FILE)
        f_size = os.path.getsize(config.COOKIE_FILE) if f_exists else 0
        return {
            "cookie_valid": self.cookie_valid,
            "cookie_file": f_exists and f_size > 100,
            "cookie_size": f_size,
            "bot_edge_pid": self.edge_pid,
            "platform": "windows" if IS_WINDOWS else "linux",
            "edge_running": self._is_running(self.edge_pid),
        }
