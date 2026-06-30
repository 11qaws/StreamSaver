import os
import sys
import json
import atexit
import subprocess
import traceback
from datetime import datetime
import asyncio
import logging
import discord
from logging.handlers import TimedRotatingFileHandler
import time

import config
from cookie_manager import CookieManager, IS_WINDOWS, _clear_lock

_NW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)  # CMD 창 숨김
from downloader import DownloadManager
from discord_bot import StreamSaverBot
from gui import GUIManager
from web_server import start as start_web
from stream_watcher import StreamWatcher
from relay_client import RelayClient
import updater

logger = logging.getLogger("StreamSaver")
CRASH_FILE = config.CRASH_FILE
PID_FILE   = os.path.join(config.BASE_DIR, "bot.pid")


# ── 단일 인스턴스 보장 ───────────────────────────────────────────────────────

def kill_competing_instances():
    """같은 main.py를 이미 실행 중인 Python 프로세스를 종료한다 (마지막 실행 우선)."""
    if not IS_WINDOWS:
        return

    current_pid = os.getpid()
    base_dir = os.path.normcase(os.path.abspath(config.BASE_DIR))
    killed = []

    # PID 파일로 이전 인스턴스 종료 (가장 안전한 방법)
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            if old_pid != current_pid:
                r = subprocess.run(
                    ["tasklist", "/fi", f"PID eq {old_pid}", "/fo", "csv", "/nh"],
                    capture_output=True, text=True, timeout=5, creationflags=_NW)
                if str(old_pid) in r.stdout:
                    subprocess.run(["taskkill", "/pid", str(old_pid), "/f"],
                                   capture_output=True, timeout=5, creationflags=_NW)
                    killed.append(old_pid)
                    logger.info("Killed previous instance: PID %d", old_pid)
        except Exception as e:
            logger.debug("PID file kill failed: %s", e)

    if killed:
        time.sleep(2.0)  # 포트/파일 해제 대기


def write_pid():
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def cleanup_pid():
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                if int(f.read().strip()) == os.getpid():
                    os.remove(PID_FILE)
    except Exception:
        pass


# ── App 컨텍스트 ─────────────────────────────────────────────────────────────

class AppContext:
    def __init__(self):
        self.gui = None
        self.cm  = None
        self.dm  = None
        self.bot = None
        self.sw  = None
        self.rc  = None   # RelayClient
        self._shutting_down = False

    def cleanup(self):
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Shutting down...")
        if self.dm:
            self.dm.shutdown()
        if self.rc:
            self.rc.stop()
        if self.sw:
            self.sw.stop()
        if self.cm:
            self.cm.stop()
        if IS_WINDOWS:
            restore_sleep()
        _clear_lock()
        cleanup_pid()
        logger.info("Cleanup done")


# ── 유틸 ────────────────────────────────────────────────────────────────────

def write_crash(exc_info):
    try:
        with open(CRASH_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} ===\n"
                    f"PID: {os.getpid()}\n")
            traceback.print_exception(*exc_info, file=f)
    except Exception:
        pass


def _migrate_files():
    """구 버전 파일 경로 → 신 경로 자동 이동 (1회성)"""
    import shutil
    moves = [
        (os.path.join(config.BASE_DIR,     "archive.txt"),         config.ARCHIVE_FILE),
        (os.path.join(config.BASE_DIR,     "cookie.txt"),          config.COOKIE_FILE),
        (os.path.join(config.BASE_DIR,     "watch_channels.json"), config.WATCH_CHANNELS_FILE),
        (os.path.join(config.BASE_DIR,     ".relay_guild"),        config.RELAY_GUILD_FILE),
        (os.path.join(config.BASE_DIR,     "crash.txt"),           config.CRASH_FILE),
        (os.path.join(config.DOWNLOAD_DIR, "history.json"),        config.HISTORY_FILE),
    ]
    for old, new in moves:
        if os.path.exists(old) and not os.path.exists(new):
            try:
                shutil.move(old, new)
                logger.info("Migrated: %s → %s", os.path.basename(old), os.path.dirname(new))
            except Exception as e:
                logger.warning("Migration failed %s: %s", os.path.basename(old), e)


def setup_logging():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    fh = TimedRotatingFileHandler(
        os.path.join(config.LOG_DIR, "streamsaver.log"),
        when="midnight", encoding="utf-8", backupCount=30)
    fh.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)

    if not getattr(sys, 'frozen', False):
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)


def startup_check():
    if IS_WINDOWS:
        if config.EDGE_PATH:
            logger.info("Edge: %s", config.EDGE_PATH)
        else:
            logger.warning("Edge not found — cookie/membership features disabled")


def prevent_sleep():
    try:
        subprocess.run(
            ["powercfg", "/requestoverride", "FUNC", "TIMER",
             "StreamSaver", "Download"],
            capture_output=True, creationflags=_NW)
    except Exception:
        pass


def restore_sleep():
    try:
        subprocess.run(
            ["powercfg", "/requestoverride", "FUNC", "TIMER",
             "StreamSaver", "Download", "/DELETE"],
            capture_output=True, creationflags=_NW)
    except Exception:
        pass


# ── 메인 ────────────────────────────────────────────────────────────────────

async def _start_relay(ctx, loop):
    """릴레이 모드: WebSocket 클라이언트 + StreamWatcher 실행"""
    ctx.rc.start(loop)
    if ctx.sw:
        ctx.sw.set_notify(lambda msg: asyncio.run_coroutine_threadsafe(
            ctx.rc._event(msg), loop))
        ctx.sw.start(loop)
    # 종료 신호 대기
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass


def main():
    ctx = AppContext()
    atexit.register(ctx.cleanup)

    setup_logging()
    _migrate_files()
    logger.info("StreamSaver starting (PID: %d)", os.getpid())

    # 다른 인스턴스 종료 → 현재 PID 기록
    kill_competing_instances()
    write_pid()

    if not config.RELAY_SERVER_URL and not config.DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set in .env (로컬 모드에서는 필수)")
        sys.exit(1)

    startup_check()
    prevent_sleep()

    ctx.gui = GUIManager()
    ctx.cm  = CookieManager()
    ctx.dm  = DownloadManager(ctx.cm)
    ctx.sw  = StreamWatcher(ctx.dm, ctx.cm)
    ctx.rc  = RelayClient(ctx.dm, ctx.cm, ctx.sw)
    ctx.gui.ctx = ctx
    ctx.gui.start_tray()

    _upd_cb = lambda info: ctx.gui.set_update_available(info) if info else None
    updater.check_update_async(_upd_cb)
    updater.check_update_loop(_upd_cb)

    try:
        start_web(ctx)
        ctx.gui.set_web_ok(True)
    except Exception as e:
        logger.error("Web server failed to start: %s", e)
        ctx.gui.add_error("web_server", "웹서버 시작 실패")
        ctx.gui.set_web_ok(False)

    def _gui_dl_watcher(event, task, **kw):
        if event == "completed":
            ctx.gui.inc_completed()
        elif event == "failed":
            ctx.gui.inc_failed()
        if event not in {"start", "completed", "failed", "cancelled"}:
            return
        try:
            s = ctx.dm.status()
            ctx.gui.set_downloading(len(s.get("active", [])))
        except Exception:
            pass

    ctx.dm.on_event(_gui_dl_watcher)

    def _update_cookie_warning():
        days = ctx.cm.cookie_days_remaining()
        if days is not None and days <= 7:
            ctx.gui.add_warning("cookie_expiry", f"쿠키 만료 {days}일 남음")
        else:
            ctx.gui.clear_warning("cookie_expiry")

    def _cm_status_changed(event, ok):
        _update_cookie_warning()

    ctx.cm.on_status_change(_cm_status_changed)

    bot = StreamSaverBot(ctx.dm, ctx.cm, stream_watcher=ctx.sw, gui=ctx.gui)
    ctx.bot = bot

    has_cookies = (os.path.exists(config.COOKIE_FILE) and
                   os.path.getsize(config.COOKIE_FILE) > 200)
    if has_cookies:
        logger.info("Existing cookie file: %d bytes", os.path.getsize(config.COOKIE_FILE))

    if IS_WINDOWS:
        ctx.cm.start_headless()
        ctx.cm.extract_cookies()
        _update_cookie_warning()
        ctx.cm.start_auto_refresh()
        if ctx.cm.cookie_valid:
            logger.info("Cookies ready via CDP")
        else:
            logger.warning("No valid cookies — use !로그인")
    else:
        ctx.cm.cookie_valid = has_cookies
        _update_cookie_warning()

    try:
        if IS_WINDOWS:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        if config.RELAY_SERVER_URL:
            # 온라인 모드 — 릴레이 서버에 WebSocket 연결
            ctx.gui.set_mode("relay")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            if config.RELAY_PAIR_CODE:
                ctx.rc.set_pair_code(config.RELAY_PAIR_CODE)

            def _rc_connected(guild_id):
                logger.info("Relay connected: guild=%s", guild_id)
                ctx.gui.notify("StreamSaver 연결됨", "릴레이 서버 연결 완료")
                ctx.gui.set_bot_connected(True)

            def _rc_disconnected():
                ctx.gui.set_bot_connected(False)

            ctx.rc.on_connect(_rc_connected)
            ctx.rc.on_disconnect(_rc_disconnected)
            ctx.rc.on_watcher_change(ctx.gui._refresh)
            ctx.rc.on_error(lambda msg: ctx.gui.notify("StreamSaver 연결", msg))

            loop.run_until_complete(_start_relay(ctx, loop))
        else:
            # 오프라인(로컬) 모드 — Discord 봇 직접 실행
            ctx.gui.set_mode("local")
            bot.run(config.DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    except discord.PrivilegedIntentsRequired:
        logger.critical("Privileged intents not enabled in Discord Developer Portal")
        write_crash(sys.exc_info())
    except discord.LoginFailure:
        logger.critical("Discord login failed — check DISCORD_TOKEN")
        write_crash(sys.exc_info())
    except Exception:
        logger.critical("Bot crashed:\n%s", traceback.format_exc())
        write_crash(sys.exc_info())
    finally:
        ctx.cleanup()
        logger.info("StreamSaver stopped")


if __name__ == "__main__":
    main()
