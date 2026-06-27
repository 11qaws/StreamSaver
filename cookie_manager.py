import subprocess
import time
import threading
import os
import platform
import logging

import config

logger = logging.getLogger("StreamSaver.CookieManager")

IS_WINDOWS = platform.system() == "Windows"


class CookieManager:
    def __init__(self):
        self.cookie_valid = False
        self._lock = threading.Lock()
        self._on_status_change = None

    def on_status_change(self, callback):
        self._on_status_change = callback

    def start_edge_minimized(self):
        if not IS_WINDOWS:
            logger.info("Edge management not supported on this OS")
            return False
        try:
            subprocess.Popen(
                ["start", "/min", "msedge", "--no-first-run",
                 "--disable-features=msAppBoundEncryption"],
                shell=True)
            logger.info("Edge started minimized")
            return True
        except Exception as e:
            logger.error(f"Failed to start Edge: {e}")
            return False

    def kill_edge(self):
        if not IS_WINDOWS:
            return True
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "msedge.exe"],
                capture_output=True, timeout=10)
            time.sleep(1.5)
            logger.info("Edge processes killed")
            return True
        except subprocess.TimeoutExpired:
            logger.warning("Timeout killing Edge")
            return False
        except Exception as e:
            logger.error(f"Failed to kill Edge: {e}")
            return False

    def extract_cookies(self):
        if not IS_WINDOWS:
            self.cookie_valid = os.path.exists(config.COOKIE_FILE) and os.path.getsize(config.COOKIE_FILE) > 200
            return self.cookie_valid

        cmd = [
            config.YT_DLP,
            "--cookies-from-browser", f"edge:{config.EDGE_PROFILE}",
            "--cookies", config.COOKIE_FILE,
            "--skip-download",
            "https://www.youtube.com/watch?v=QHsG4CgPblE",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30)
            if (os.path.exists(config.COOKIE_FILE) and
                    os.path.getsize(config.COOKIE_FILE) > 200):
                self.cookie_valid = True
                logger.info("Cookies extracted successfully")
                return True
            logger.warning(
                f"Cookie file too small: "
                f"{os.path.getsize(config.COOKIE_FILE) if os.path.exists(config.COOKIE_FILE) else 0}B")
            self.cookie_valid = False
            return False
        except Exception as e:
            logger.error(f"Cookie extraction failed: {e}")
            self.cookie_valid = False
            return False

    def refresh_cookies(self):
        with self._lock:
            logger.info("Cookie refresh cycle starting...")
            self.kill_edge()
            success = self.extract_cookies()
            if IS_WINDOWS:
                self.start_edge_minimized()
            if success:
                logger.info("Cookie refresh completed successfully")
            else:
                logger.warning("Cookie refresh failed, will retry next cycle")
            if self._on_status_change:
                self._on_status_change("cookie_refresh", success)
            return success

    def start_auto_refresh(self):
        if not IS_WINDOWS:
            logger.info("Auto cookie refresh disabled on this OS")
            return

        def _loop():
            time.sleep(30)
            self.refresh_cookies()
            while True:
                time.sleep(config.COOKIE_REFRESH_INTERVAL)
                self.refresh_cookies()

        thread = threading.Thread(target=_loop, daemon=True)
        thread.start()
        logger.info("Auto cookie refresh started every 30min")

    def login_flow(self, on_done=None):
        if not IS_WINDOWS:
            logger.info("Login flow not supported on this OS")
            if on_done:
                on_done(False)
            return

        def _wait_for_close():
            self.kill_edge()
            extant = self.extract_cookies()
            if extant:
                self.start_edge_minimized()
                logger.info("Existing cookies still valid, no login needed")
                if on_done:
                    on_done(True)
                return

            subprocess.Popen(
                ["start", "msedge", "--no-first-run",
                 "--disable-features=msAppBoundEncryption"],
                shell=True)
            logger.info("Edge opened for user login")

            while True:
                r = subprocess.run(
                    ["tasklist", "/fi", "imagename eq msedge.exe"],
                    capture_output=True, text=True)
                if "msedge.exe" not in r.stdout:
                    break
                time.sleep(2)
            time.sleep(1)

            success = self.extract_cookies()
            self.start_edge_minimized()
            if success:
                logger.info("Login successful, cookies extracted")
            else:
                logger.warning("Login may have failed")
            if on_done:
                on_done(success)

        thread = threading.Thread(target=_wait_for_close, daemon=True)
        thread.start()

    def get_status(self):
        return {
            "cookie_valid": self.cookie_valid,
            "cookie_file": (
                os.path.exists(config.COOKIE_FILE) and
                os.path.getsize(config.COOKIE_FILE) > 100
            ),
            "platform": "windows" if IS_WINDOWS else "linux",
        }
