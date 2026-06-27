import subprocess
import time
import threading
import os
import platform
import signal
import logging

import config

logger = logging.getLogger("StreamSaver.CookieManager")

IS_WINDOWS = platform.system() == "Windows"
EDGE_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-extensions",
    "--disable-gpu",
    "--disable-features=msAppBoundEncryption",
]


class CookieManager:
    def __init__(self):
        self.cookie_valid = False
        self.edge_pid = None
        self._lock = threading.Lock()
        self._on_status_change = None

    def on_status_change(self, callback):
        self._on_status_change = callback

    def start_headless(self):
        if not IS_WINDOWS:
            return False
        try:
            profile = config.BOT_EDGE_PROFILE
            os.makedirs(profile, exist_ok=True)
            proc = subprocess.Popen(
                ["msedge", f"--user-data-dir={profile}",
                 "--headless=new", *EDGE_FLAGS,
                 "https://www.youtube.com"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            self.edge_pid = proc.pid
            logger.info("Edge headless started (PID: %d)", self.edge_pid)
            return True
        except Exception as e:
            logger.error("Failed to start Edge headless: %s", e)
            self.edge_pid = None
            return False

    def _kill_bot_edge(self):
        if not self.edge_pid:
            return True
        try:
            if IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/f", "/pid", str(self.edge_pid)],
                    capture_output=True, timeout=5)
            else:
                os.kill(self.edge_pid, signal.SIGTERM)
            self.edge_pid = None
            time.sleep(1)
            return True
        except subprocess.TimeoutExpired:
            logger.warning("Timeout killing bot Edge PID %d", self.edge_pid)
            return False
        except Exception as e:
            logger.error("Failed to kill bot Edge: %s", e)
            self.edge_pid = None
            return False

    def start_visible(self):
        if not IS_WINDOWS:
            return False
        try:
            profile = config.BOT_EDGE_PROFILE
            os.makedirs(profile, exist_ok=True)
            proc = subprocess.Popen(
                ["msedge", f"--user-data-dir={profile}",
                 *EDGE_FLAGS,
                 "https://www.youtube.com"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            self.edge_pid = proc.pid
            logger.info("Edge visible started for login (PID: %d)", self.edge_pid)
            return True
        except Exception as e:
            logger.error("Failed to start Edge visible: %s", e)
            self.edge_pid = None
            return False

    def extract_cookies(self):
        if not IS_WINDOWS:
            f_exists = os.path.exists(config.COOKIE_FILE)
            f_size = os.path.getsize(config.COOKIE_FILE) if f_exists else 0
            self.cookie_valid = f_size > 200
            return self.cookie_valid

        self._kill_bot_edge()
        os.makedirs(config.BOT_EDGE_PROFILE, exist_ok=True)

        cmd = [
            config.YT_DLP,
            "--cookies-from-browser", f"edge:{config.BOT_EDGE_PROFILE}",
            "--cookies", config.COOKIE_FILE,
            "--skip-download",
            "https://www.youtube.com/watch?v=QHsG4CgPblE",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30)
            f_exists = os.path.exists(config.COOKIE_FILE)
            f_size = os.path.getsize(config.COOKIE_FILE) if f_exists else 0
            if f_size > 200:
                self.cookie_valid = True
                logger.info("Cookies extracted successfully (%d bytes)", f_size)
                self.start_headless()
                return True
            logger.warning(
                "Cookie file too small: %d bytes", f_size)
            self.cookie_valid = False
            self.start_headless()
            return False
        except Exception as e:
            logger.error("Cookie extraction failed: %s", e)
            self.cookie_valid = False
            self.start_headless()
            return False

    def refresh_cookies(self):
        with self._lock:
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
        if not IS_WINDOWS:
            return

        def _loop():
            if not self.cookie_valid:
                self.refresh_cookies()
            while True:
                time.sleep(config.COOKIE_REFRESH_INTERVAL)
                self.refresh_cookies()

        thread = threading.Thread(target=_loop, daemon=True)
        thread.start()
        logger.info("Auto cookie refresh every 30min")

    def login_flow(self, on_done=None):
        if not IS_WINDOWS:
            if on_done:
                on_done(False)
            return

        def _wait_for_close():
            self._kill_bot_edge()

            extant = os.path.exists(config.COOKIE_FILE)
            if extant and self.cookie_valid:
                logger.info("Existing cookies still valid, no login needed")
                self.start_headless()
                if on_done:
                    on_done(True)
                return

            self.start_visible()

            if self.edge_pid:
                try:
                    while True:
                        if IS_WINDOWS:
                            r = subprocess.run(
                                ["tasklist", "/fi", f"PID eq {self.edge_pid}"],
                                capture_output=True, text=True)
                            if str(self.edge_pid) not in r.stdout:
                                break
                        else:
                            try:
                                os.kill(self.edge_pid, 0)
                            except ProcessLookupError:
                                break
                        time.sleep(2)
                except Exception:
                    pass

            time.sleep(1)
            self.edge_pid = None

            success = self.extract_cookies()
            if success:
                logger.info("Login successful")
            else:
                logger.warning("Login may have failed")
            if on_done:
                on_done(success)

        thread = threading.Thread(target=_wait_for_close, daemon=True)
        thread.start()

    def get_status(self):
        f_exists = os.path.exists(config.COOKIE_FILE)
        f_size = os.path.getsize(config.COOKIE_FILE) if f_exists else 0
        return {
            "cookie_valid": self.cookie_valid,
            "cookie_file": f_exists and f_size > 100,
            "cookie_size": f_size,
            "bot_edge_pid": self.edge_pid,
            "platform": "windows" if IS_WINDOWS else "linux",
        }
