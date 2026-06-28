import logging
import os
import shutil
import sys
import subprocess
import threading
import time
import webbrowser
from enum import Enum

import config

logger = logging.getLogger("StreamSaver.GUI")
_NW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_NAME = "StreamSaver"


class TrayState(Enum):
    IDLE        = "idle"         # 초록  — 정상 대기
    DOWNLOADING = "downloading"  # 파랑  — 다운로드 진행 중
    WARNING     = "warning"      # 노랑  — 쿠키 만료 임박 등
    ERROR       = "error"        # 빨강  — 오류
    OFFLINE     = "offline"      # 회색  — Discord 미연결


# ── 아이콘 이미지 ─────────────────────────────────────────────────────────────

def _make_icon(state: TrayState):
    from PIL import Image, ImageDraw
    S = 64
    img  = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = {
        TrayState.IDLE:        (45,  200,  80),
        TrayState.DOWNLOADING: (40,  140, 220),
        TrayState.WARNING:     (220, 175,   0),
        TrayState.ERROR:       (210,  50,  50),
        TrayState.OFFLINE:     (145, 145, 145),
    }[state]
    draw.ellipse([3, 3, 61, 61], fill=color)
    w = "white"
    if state == TrayState.DOWNLOADING:
        # 아래 화살표
        draw.rectangle([27, 16, 37, 36], fill=w)
        draw.polygon([(18, 34), (32, 50), (46, 34)], fill=w)
    elif state == TrayState.WARNING:
        # 느낌표
        draw.rectangle([28, 16, 36, 38], fill=w)
        draw.ellipse([28, 44, 36, 52], fill=w)
    elif state == TrayState.ERROR:
        # X
        draw.line([18, 18, 46, 46], fill=w, width=7)
        draw.line([46, 18, 18, 46], fill=w, width=7)
    elif state == TrayState.OFFLINE:
        # 빗금
        draw.line([46, 16, 18, 48], fill=w, width=7)
    return img


# ── 자동시작 레지스트리 ───────────────────────────────────────────────────────

def _autostart_is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
            winreg.QueryValueEx(k, _REG_NAME)
        return True
    except OSError:
        return False


def _autostart_set(enable: bool):
    if sys.platform != "win32":
        return
    import winreg
    vbs = os.path.join(config.BASE_DIR, "run_silent.vbs")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, _REG_NAME, 0, winreg.REG_SZ,
                                  f'wscript.exe "{vbs}"')
            else:
                try:
                    winreg.DeleteValue(k, _REG_NAME)
                except FileNotFoundError:
                    pass
        logger.info("Autostart %s", "enabled" if enable else "disabled")
    except Exception as e:
        logger.error("Autostart registry error: %s", e)


# ── GUIManager ───────────────────────────────────────────────────────────────

class GUIManager:
    def __init__(self):
        self.ctx              = None
        self._icon            = None       # pystray.Icon — setup 콜백 후 세팅
        self._running         = False
        self._lock            = threading.Lock()
        self._bot_connected   = False
        self._active_dl       = 0
        self._warnings        = {}         # key → str
        self._errors          = {}         # key → str
        self._start_time      = time.time()
        self._completed       = 0
        self._failed          = 0

    # ── 상태 계산 ─────────────────────────────────────────────────────────────

    def _state(self) -> TrayState:
        if not self._bot_connected:
            return TrayState.OFFLINE
        with self._lock:
            if self._errors:
                return TrayState.ERROR
            if self._active_dl > 0:
                return TrayState.DOWNLOADING
            if self._warnings:
                return TrayState.WARNING
        return TrayState.IDLE

    def _refresh(self):
        """아이콘·툴팁 즉시 갱신"""
        icon = self._icon
        if not icon:
            return
        s = self._state()
        try:
            icon.icon  = _make_icon(s)
            icon.title = self._tooltip(s)
        except Exception as e:
            logger.debug("Icon refresh error: %s", e)

    # ── 외부에서 호출하는 상태 변경 API ──────────────────────────────────────

    def set_bot_connected(self, connected: bool):
        self._bot_connected = connected
        self._refresh()

    def set_downloading(self, count: int):
        self._active_dl = max(0, count)
        self._refresh()

    def add_warning(self, key: str, msg: str):
        with self._lock:
            self._warnings[key] = msg
        self._refresh()

    def clear_warning(self, key: str):
        with self._lock:
            self._warnings.pop(key, None)
        self._refresh()

    def add_error(self, key: str, msg: str):
        with self._lock:
            self._errors[key] = msg
        self._refresh()

    def clear_error(self, key: str):
        with self._lock:
            self._errors.pop(key, None)
        self._refresh()

    def inc_completed(self):
        self._completed += 1

    def inc_failed(self):
        self._failed += 1

    # ── 알림 ─────────────────────────────────────────────────────────────────

    def notify(self, title: str, message: str = ""):
        icon = self._icon
        if icon:
            try:
                # pystray: notify(message, title)
                icon.notify(message or " ", title)
            except Exception as e:
                logger.debug("Notify error: %s", e)

    # ── 툴팁 텍스트 ───────────────────────────────────────────────────────────

    def _tooltip(self, state: TrayState | None = None) -> str:
        if state is None:
            state = self._state()
        label = {
            TrayState.IDLE:        "대기 중",
            TrayState.DOWNLOADING: "다운로드 중",
            TrayState.WARNING:     "경고",
            TrayState.ERROR:       "오류 발생",
            TrayState.OFFLINE:     "봇 미연결",
        }[state]
        lines = [f"StreamSaver  ·  {label}"]

        # 다운로드 현황
        dm = self.ctx.dm if self.ctx else None
        if dm:
            s = dm.status()
            active = s.get("active", [])
            if active:
                speeds = [t["speed"] for t in active if t.get("speed")]
                vod    = [t["progress"] for t in active
                          if t.get("state") != "live" and t.get("progress") is not None]
                spd = speeds[0] if speeds else "?"
                if vod:
                    lines.append(f"⬇ {len(active)}개 · {sum(vod)/len(vod):.0f}% · {spd}")
                else:
                    lines.append(f"⬇ {len(active)}개 라이브 · {spd}")
            elif s.get("queued", 0):
                lines.append(f"🕐 대기 {s['queued']}개")
            else:
                lines.append("📭 대기 없음")

        # 쿠키·Edge 상태
        cm = self.ctx.cm if self.ctx else None
        if cm:
            ck   = "쿠키 ✅" if cm.cookie_valid else "쿠키 ❌"
            edge = "Edge 🟢" if (cm.cdp_port and cm._is_running()) else "Edge 🔴"
            lines.append(f"{ck}  {edge}")

        # 경고/오류
        with self._lock:
            alerts = list(self._warnings.values()) + list(self._errors.values())
        for a in alerts[:2]:
            lines.append(f"⚠ {a}")

        return "\n".join(lines)

    # ── 메뉴 ─────────────────────────────────────────────────────────────────

    def _detail_items(self):
        """📋 상세정보 서브메뉴 — 업타임·디스크·통계"""
        import pystray

        # 업타임
        elapsed = int(time.time() - self._start_time)
        h, rem  = divmod(elapsed, 3600)
        m       = rem // 60
        uptime  = f"{h}시간 {m}분" if h else f"{m}분"
        yield pystray.MenuItem(f"⏱ 가동 {uptime}", None, enabled=False)

        # 디스크 여유 공간
        try:
            dl_dir = config.DOWNLOAD_DIR
            target = dl_dir if os.path.exists(dl_dir) else (os.path.splitdrive(dl_dir)[0] + "\\")
            free_gb = shutil.disk_usage(target).free / 1024 ** 3
            disk_txt = f"💾 디스크 {free_gb:.1f} GB 남음"
        except Exception:
            disk_txt = "💾 디스크 확인 불가"
        yield pystray.MenuItem(disk_txt, None, enabled=False)

        # 세션 통계
        yield pystray.MenuItem(
            f"📊 완료 {self._completed} · 실패 {self._failed}",
            None, enabled=False)

    def _menu_items(self):
        """pystray가 메뉴 표시 시점에 호출 — 매번 최신 상태 반영"""
        import pystray

        # ① 대시보드 (더블클릭 기본 액션)
        yield pystray.MenuItem(
            "🌐 대시보드 열기",
            lambda icon, item: webbrowser.open(f"http://localhost:{config.WEB_PORT}"),
            default=True,
        )
        yield pystray.Menu.SEPARATOR

        # ② 다운로드 현황 (동적)
        dm = self.ctx.dm if self.ctx else None
        if dm:
            s      = dm.status()
            active = s.get("active", [])
            if active:
                for t in active[:5]:
                    if t.get("state") == "live":
                        txt = f"📡 #{t['id']}  {t.get('downloaded') or '?'} · {t.get('speed') or '?'}"
                    else:
                        txt = f"⬇ #{t['id']}  {t.get('progress', 0):.0f}% · {t.get('speed') or '?'}"
                    yield pystray.MenuItem(txt, None, enabled=False)
                if s.get("queued", 0):
                    yield pystray.MenuItem(f"🕐 대기 {s['queued']}개", None, enabled=False)
            else:
                yield pystray.MenuItem("📭 다운로드 없음", None, enabled=False)
        else:
            yield pystray.MenuItem("⏳ 초기화 중...", None, enabled=False)

        # ③ 경고·오류
        with self._lock:
            alerts = list(self._errors.values()) + list(self._warnings.values())
        if alerts:
            yield pystray.Menu.SEPARATOR
            for a in alerts[:3]:
                yield pystray.MenuItem(f"⚠ {a}", None, enabled=False)

        # ④ 컨트롤
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(
            "📂 다운로드 폴더",
            lambda icon, item: subprocess.Popen(
                ["explorer", config.DOWNLOAD_DIR], creationflags=_NW),
        )
        yield pystray.MenuItem("📋 상세정보", pystray.Menu(self._detail_items))
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("🔄 봇 재시작", self._on_restart)
        yield pystray.MenuItem(
            "🚀 시작 시 자동 실행",
            lambda icon, item: _autostart_set(not _autostart_is_enabled()),
            checked=lambda item: _autostart_is_enabled(),
        )
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("⏹ 종료", self._on_quit)

    # ── 트레이 구동 ───────────────────────────────────────────────────────────

    def start_tray(self):
        try:
            import pystray
        except ImportError as e:
            logger.error("pystray not available: %s", e)
            return

        gui = self

        def _setup(icon):
            gui._icon    = icon
            gui._running = True
            icon.visible = True   # 커스텀 setup 사용 시 명시적으로 표시해야 함
            logger.info("Tray icon active")
            # _setup 확정 후 ticker 시작 — _running=True 보장된 시점
            def _ticker():
                while gui._running:
                    time.sleep(5)
                    gui._refresh()
            threading.Thread(target=_ticker, daemon=True).start()

        def _run():
            try:
                icon = pystray.Icon(
                    "streamsaver",
                    _make_icon(TrayState.OFFLINE),
                    "StreamSaver  ·  시작 중...",
                    menu=pystray.Menu(gui._menu_items),
                )
                logger.info("Tray icon created, starting message loop")
                icon.run(setup=_setup)
            except Exception as e:
                logger.error("Tray error: %s", e, exc_info=True)

        threading.Thread(target=_run, daemon=True, name="TrayThread").start()
        logger.info("Tray thread started")

    # ── 재시작 / 종료 ─────────────────────────────────────────────────────────

    def _on_restart(self, icon, item=None):
        logger.info("Tray: restart")
        self._running = False
        icon.stop()
        # cleanup 먼저 → 포트·락·Edge 해제 후 새 프로세스 시작
        if self.ctx:
            self.ctx.cleanup()
        time.sleep(1.5)   # OS가 포트 반환할 시간
        try:
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0) |
                     getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                     _NW)
            subprocess.Popen(
                [sys.executable, os.path.join(config.BASE_DIR, "main.py")],
                creationflags=flags, cwd=config.BASE_DIR)
        except Exception as e:
            logger.error("Restart failed: %s", e)
        os._exit(0)

    def _on_quit(self, icon, item=None):
        logger.info("Tray: quit")
        self._running = False
        icon.stop()
        if self.ctx:
            self.ctx.cleanup()
        os._exit(0)
