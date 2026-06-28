import os
import sys
import traceback
import asyncio
import logging
import discord
from logging.handlers import TimedRotatingFileHandler

import config
from cookie_manager import CookieManager, IS_WINDOWS, _clear_lock
from downloader import DownloadManager
from discord_bot import StreamSaverBot
from gui import GUIManager
from web_server import start as start_web

logger = logging.getLogger("StreamSaver")
PID_FILE = os.path.join(config.BASE_DIR, "bot.pid")


def check_single_instance():
    if not os.path.exists(PID_FILE):
        return True
    try:
        with open(PID_FILE) as f:
            old_pid = int(f.read().strip())
        if IS_WINDOWS:
            import subprocess
            r = subprocess.run(
                ["tasklist", "/fi", f"PID eq {old_pid}"],
                capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if str(old_pid) in line and ("python" in line.lower()):
                    logger.error("Bot already running (PID: %d). Exiting.", old_pid)
                    return False
        else:
            os.kill(old_pid, 0)
            logger.error("Bot already running (PID: %d). Exiting.", old_pid)
            return False
    except (ValueError, ProcessLookupError, Exception):
        pass
    return True


def write_pid():
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def remove_pid():
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def setup_logging():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    handler = TimedRotatingFileHandler(
        os.path.join(config.LOG_DIR, "streamsaver.log"),
        when="midnight", encoding="utf-8", backupCount=30)
    handler.setFormatter(fmt)

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)


def prevent_sleep():
    try:
        import subprocess
        subprocess.run(
            ["powercfg", "/requestoverride", "FUNC", "TIMER",
             "StreamSaver", "Download"],
            capture_output=True)
        logger.info("Sleep prevention active")
    except Exception as e:
        logger.warning(f"Sleep prevention failed: {e}")


def restore_sleep():
    try:
        import subprocess
        subprocess.run(
            ["powercfg", "/requestoverride", "FUNC", "TIMER",
             "StreamSaver", "Download", "/DELETE"],
            capture_output=True)
        logger.info("Sleep prevention removed")
    except Exception as e:
        logger.warning(f"Sleep restore failed: {e}")


def main():
    setup_logging()
    logger.info("StreamSaver starting... (PID: %d)", os.getpid())

    if not check_single_instance():
        sys.exit(0)

    if not config.DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set in .env")
        print("ERROR: DISCORD_TOKEN not set. Copy .env.example to .env and add your token.")
        sys.exit(1)

    write_pid()
    prevent_sleep()
    start_web()

    gui = GUIManager()
    gui.start_tray()

    cm = CookieManager()
    dm = DownloadManager(cm)

    bot = StreamSaverBot(dm, cm)

    has_cookies = (os.path.exists(config.COOKIE_FILE) and
                   os.path.getsize(config.COOKIE_FILE) > 200)
    if has_cookies:
        logger.info("Cookie file found (%d bytes)", os.path.getsize(config.COOKIE_FILE))

    if IS_WINDOWS:
        cm.start_headless()
        cm.extract_cookies()
        cm.start_auto_refresh()
        if cm.cookie_valid:
            logger.info("Cookies extracted via CDP")
        else:
            logger.warning("No valid cookies. Use !로그인 to set up YouTube login.")
    else:
        cm.cookie_valid = has_cookies
        if has_cookies:
            logger.info("Linux mode: using existing cookies")

    gui.notify("StreamSaver", "봇이 시작되었습니다")

    try:
        if IS_WINDOWS:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        bot.run(config.DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    except discord.PrivilegedIntentsRequired:
        logger.critical("Privileged intents not enabled. Enable MESSAGE CONTENT INTENT in Discord Developer Portal.")
        sys.exit(1)
    except discord.LoginFailure:
        logger.critical("Login failed. Check DISCORD_TOKEN in .env")
        sys.exit(1)
    except Exception as e:
        logger.critical("Bot crashed: %s", traceback.format_exc())
        sys.exit(1)
    else:
        logger.warning("Bot.run() returned without exception (unexpected)")
        sys.exit(43)
    finally:
        cm.stop()
        restore_sleep()
        _clear_lock()
        remove_pid()
        logger.info("StreamSaver stopped")


if __name__ == "__main__":
    main()
