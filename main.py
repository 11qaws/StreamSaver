import os
import sys
import logging
from logging.handlers import TimedRotatingFileHandler

import config
from cookie_manager import CookieManager
from downloader import DownloadManager
from discord_bot import StreamSaverBot
from gui import GUIManager
from web_server import start as start_web

logger = logging.getLogger("StreamSaver")


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
    logger.info("StreamSaver starting...")

    if not config.DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set in .env")
        print("ERROR: DISCORD_TOKEN not set. Copy .env.example to .env and add your token.")
        sys.exit(1)

    prevent_sleep()
    start_web()

    gui = GUIManager()
    gui.start_tray()

    cm = CookieManager()
    dm = DownloadManager(cm)

    bot = StreamSaverBot(dm, cm)

    cm.start_edge_minimized()
    cm.extract_cookies()
    cm.start_auto_refresh()

    gui.notify("StreamSaver", "봇이 시작되었습니다")

    try:
        bot.run(config.DISCORD_TOKEN)
    except KeyboardInterrupt:
        pass
    finally:
        restore_sleep()
        logger.info("StreamSaver stopped")


if __name__ == "__main__":
    main()
