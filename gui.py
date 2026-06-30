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
    UPDATE      = "update"       # 보라  — 업데이트 있음


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
        TrayState.UPDATE:      (130,  80, 210),
    }[state]
    draw.ellipse([3, 3, 61, 61], fill=color)
    w = "white"
    if state == TrayState.DOWNLOADING:
        # 아래 화살표
        draw.rectangle([27, 16, 37, 36], fill=w)
        draw.polygon([(18, 34), (32, 50), (46, 34)], fill=w)
    elif state == TrayState.UPDATE:
        # 위 화살표
        draw.rectangle([27, 30, 37, 50], fill=w)
        draw.polygon([(18, 32), (32, 14), (46, 32)], fill=w)
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
        self._mode            = "relay" if config.RELAY_SERVER_URL else "local"
        self._update_info     = None   # {"version": ..., "url": ...} or None
        self._update_progress = None   # None=대기, int=다운로드 진행률(0~100)
        self._web_ok          = True   # 웹서버 정상 기동 여부

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
        if self._update_info and self._update_progress is None:
            return TrayState.UPDATE
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

    def set_mode(self, mode: str):
        """'relay' 또는 'local'"""
        self._mode = mode
        self._refresh()

    def set_bot_connected(self, connected: bool):
        self._bot_connected = connected
        self._refresh()

    def set_web_ok(self, ok: bool):
        self._web_ok = ok
        self._refresh()

    def set_update_available(self, info: dict):
        self._update_info = info
        self.notify("StreamSaver 업데이트",
                    f"v{info['version']} 업데이트가 있습니다. 트레이 메뉴에서 확인하세요.")
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
        mode_label = "🌐 온라인" if self._mode == "relay" else "💻 로컬"
        lines = [f"StreamSaver  ·  {mode_label}  ·  {label}"]

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

    def _watcher_items(self):
        """📡 Unarchived 서브메뉴"""
        import pystray
        sw = self.ctx.sw if self.ctx else None
        if sw is None:
            yield pystray.MenuItem("기능 비활성화", None, enabled=False)
            return
        channels = sw.list_channels()
        if not channels:
            yield pystray.MenuItem("등록된 채널 없음", None, enabled=False)
        else:
            yield pystray.MenuItem(f"감시 중  {len(channels)}채널", None, enabled=False)
            for _url, info in channels:
                from stream_watcher import StreamWatcher
                filt  = info["title_filter"] or "전체"
                label = StreamWatcher.display_name(info)
                yield pystray.MenuItem(f"  • {label}  [{filt}]", None, enabled=False)
        interval_min = config.WATCH_POLL_INTERVAL // 60
        yield pystray.MenuItem(f"⏱ {interval_min}분 간격 폴링", None, enabled=False)

    def _settings_items(self):
        """⚙️ 설정 서브메뉴"""
        import pystray
        yield pystray.MenuItem(
            "🚀 시작 시 자동 실행",
            lambda icon, item: _autostart_set(not _autostart_is_enabled()),
            checked=lambda item: _autostart_is_enabled(),
        )
        yield pystray.Menu.SEPARATOR
        # 상세 정보
        elapsed = int(time.time() - self._start_time)
        h, rem  = divmod(elapsed, 3600)
        m       = rem // 60
        uptime  = f"{h}시간 {m}분" if h else f"{m}분"
        yield pystray.MenuItem(f"⏱ 가동 {uptime}", None, enabled=False)
        try:
            dl_dir  = config.DOWNLOAD_DIR
            target  = dl_dir if os.path.exists(dl_dir) else (os.path.splitdrive(dl_dir)[0] + "\\")
            free_gb = shutil.disk_usage(target).free / 1024 ** 3
            disk_txt = f"💾 디스크 {free_gb:.1f} GB 남음"
        except Exception:
            disk_txt = "💾 디스크 확인 불가"
        yield pystray.MenuItem(disk_txt, None, enabled=False)
        yield pystray.MenuItem(
            f"📊 완료 {self._completed} · 실패 {self._failed}", None, enabled=False)
        cm = self.ctx.cm if self.ctx else None
        if cm:
            ck   = "쿠키 ✅" if cm.cookie_valid else "쿠키 ❌"
            edge = "Edge 🟢" if (cm.cdp_port and cm._is_running()) else "Edge 🔴"
            yield pystray.MenuItem(f"{ck}  {edge}", None, enabled=False)

    def _menu_items(self):
        """pystray가 메뉴 표시 시점에 호출 — 매번 최신 상태 반영"""
        import pystray

        # ① 상태
        rc = self.ctx.rc if self.ctx else None
        if self._mode == "relay":
            if rc and rc.connected:
                mode_txt = "🌐 온라인 모드  —  연결됨"
            elif rc and rc.guild_id:
                mode_txt = "🔄 온라인 모드  —  재연결 중..."
            else:
                mode_txt = "🔌 온라인 모드  —  연결 필요"
        else:
            mode_txt = "💻 로컬 모드" + ("  —  연결됨" if self._bot_connected else "  —  미연결")
        yield pystray.MenuItem(mode_txt, None, enabled=False)

        if self._mode == "relay" and not (rc and rc.connected):
            if not (rc and rc.guild_id):
                # 첫 설치 또는 리셋 — 봇 초대 + 서버 연결 모두 표시
                invite_url = getattr(config, "BOT_INVITE_URL", "")
                if invite_url:
                    yield pystray.MenuItem(
                        "🤖 1단계: Discord 봇 서버 초대",
                        lambda icon, item: self._open_url(invite_url),
                    )
                yield pystray.MenuItem("🔗 2단계: 서버 연결", self._connect_server)
            # guild_id 있으면 자동 재연결 중 — 버튼 없음
        yield pystray.Menu.SEPARATOR

        # ② 주요 액션
        yield pystray.MenuItem(
            "📊 대시보드 열기" if self._web_ok else "📊 대시보드 (시작 실패)",
            lambda icon, item: webbrowser.open(f"http://localhost:{config.WEB_PORT}"),
            default=True,
            enabled=self._web_ok,
        )
        yield pystray.MenuItem(
            "📂 다운로드 폴더",
            lambda icon, item: self._open_download_folder(),
        )
        yield pystray.Menu.SEPARATOR

        # ③ 다운로드 현황
        dm = self.ctx.dm if self.ctx else None
        if dm:
            s      = dm.status()
            active = s.get("active", [])
            if active:
                for t in active[:5]:
                    tid = t["id"]
                    if t.get("state") == "live":
                        txt = f"📡 #{tid}  {t.get('downloaded') or '?'} · {t.get('speed') or '?'}  ✕"
                    else:
                        txt = f"⬇ #{tid}  {t.get('progress', 0):.0f}% · {t.get('speed') or '?'}  ✕"
                    yield pystray.MenuItem(
                        txt,
                        lambda icon, item, _id=tid: self._cancel_task(_id),
                    )
                if s.get("queued", 0):
                    yield pystray.MenuItem(f"🕐 대기 {s['queued']}개", None, enabled=False)
            else:
                yield pystray.MenuItem("📭 다운로드 없음", None, enabled=False)
        else:
            yield pystray.MenuItem("⏳ 초기화 중...", None, enabled=False)

        # ④ 경고·오류
        with self._lock:
            alerts = list(self._errors.values()) + list(self._warnings.values())
        if alerts:
            yield pystray.Menu.SEPARATOR
            for a in alerts[:3]:
                yield pystray.MenuItem(f"⚠ {a}", None, enabled=False)

        # ⑤ 서브메뉴
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("📡 Unarchived", pystray.Menu(self._watcher_items))
        yield pystray.MenuItem("⚙️ 설정", pystray.Menu(self._settings_items))
        yield pystray.Menu.SEPARATOR

        # ⑥ 하단
        yield pystray.MenuItem("🔄 재시작", self._on_restart)
        if self._update_info:
            v = self._update_info["version"]
            if self._update_progress is not None:
                pct = self._update_progress
                label = (f"⏳ 설치 중..." if pct >= 100
                         else f"⬇️ 업데이트 다운로드 중 ({pct}%)")
                yield pystray.MenuItem(label, None, enabled=False)
            else:
                yield pystray.MenuItem(
                    f"⬆️ v{v} 업데이트",
                    self._start_auto_update,
                )
        yield pystray.MenuItem(
            f"ℹ️ StreamSaver v{config.APP_VERSION}", None, enabled=False)
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

    def _connect_server(self, icon, item=None):
        def _dialog():
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()

            dlg = tk.Toplevel(root)
            dlg.title("서버 연결")
            dlg.resizable(False, False)
            dlg.attributes("-topmost", True)
            dlg.grab_set()

            pad = {"padx": 18, "pady": 6}

            tk.Label(dlg, text="Discord에서 /setup 입력 후 받은 코드를 입력하세요.",
                     justify="center", **pad).pack()
            tk.Label(dlg, text="아직 /setup을 실행하지 않았다면 먼저 Discord를 여세요.",
                     foreground="#666", font=("", 9), **pad).pack()

            tk.Button(
                dlg, text="🔗 Discord 열기",
                command=lambda: webbrowser.open("https://discord.com/app"),
                relief="groove",
            ).pack(pady=(4, 10))

            tk.Label(dlg, text="코드 (예: ABC-123)", **pad).pack()
            entry = tk.Entry(dlg, width=16, font=("Consolas", 15), justify="center")
            entry.pack(padx=18, pady=4)
            entry.focus_set()

            result = {"code": None}

            def _submit(e=None):
                result["code"] = entry.get()
                dlg.destroy()

            def _cancel(e=None):
                dlg.destroy()

            entry.bind("<Return>", _submit)
            dlg.protocol("WM_DELETE_WINDOW", _cancel)

            btn_frame = tk.Frame(dlg)
            btn_frame.pack(pady=10)
            tk.Button(btn_frame, text="연결", width=8, command=_submit).pack(side="left", padx=4)
            tk.Button(btn_frame, text="취소", width=8, command=_cancel).pack(side="left", padx=4)

            root.mainloop()

            code = (result["code"] or "").strip().upper()
            if not code:
                return
            rc = self.ctx.rc if self.ctx else None
            if rc:
                rc.set_pair_code(code)
            self._update_env_key("RELAY_PAIR_CODE", code)
            self.notify("StreamSaver", f"연결 시도 중... ({code})")

        threading.Thread(target=_dialog, daemon=True).start()

    def _update_env_key(self, key: str, value: str):
        env_path = os.path.join(config.BASE_DIR, ".env")
        try:
            lines = []
            found = False
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith(f"{key}="):
                            lines.append(f"{key}={value}\n")
                            found = True
                        else:
                            lines.append(line)
            if not found:
                lines.append(f"{key}={value}\n")
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception as e:
            logger.error("env update error: %s", e)

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

    def _open_url(self, url: str):
        import webbrowser
        webbrowser.open(url)

    def _open_download_folder(self):
        os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
        subprocess.Popen(["explorer", config.DOWNLOAD_DIR], creationflags=_NW)

    def _cancel_task(self, task_id: int):
        dm = self.ctx.dm if self.ctx else None
        if dm and dm.cancel(task_id):
            self.notify("StreamSaver", f"#{task_id} 취소 중...")
        else:
            self.notify("StreamSaver", f"#{task_id} 취소 불가 — 이미 완료됐거나 없는 작업입니다.")

    def _start_auto_update(self, icon, item=None):
        if self._update_progress is not None:
            return  # 이미 진행 중
        threading.Thread(target=self._run_auto_update, daemon=True,
                         name="AutoUpdater").start()

    def _run_auto_update(self):
        import updater
        info = self._update_info
        if not info:
            return
        url = info["url"]

        def on_progress(pct: int):
            self._update_progress = pct
            self._refresh()

        try:
            self._update_progress = 0
            self._refresh()
            installer_path = updater.download_update(url, on_progress)
            self._update_progress = 100
            self._refresh()
            self.notify("StreamSaver", "업데이트를 설치합니다. 잠시 후 재시작됩니다.")
            updater.install_update(installer_path)
            # 인스톨러가 이 프로세스를 종료하고 새 버전을 실행함
        except Exception as e:
            logger.error("Auto-update failed: %s", e)
            self._update_progress = None
            self.notify("StreamSaver", f"업데이트 실패: {e}")
            self._refresh()

    def _on_quit(self, icon, item=None):
        logger.info("Tray: quit")
        self._running = False
        icon.stop()
        if self.ctx:
            self.ctx.cleanup()
        os._exit(0)
