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


_ANIM_FRAMES   = 4
_ANIM_INTERVAL = 0.15   # 초 단위 — 프레임 간격


# ── 아이콘 이미지 ─────────────────────────────────────────────────────────────

def _make_icon(state: TrayState, frame: int = 0):
    from PIL import Image, ImageDraw
    S = 64
    img  = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bg = {
        TrayState.IDLE:        (37,   99, 235),   # 대시보드 accent #2563EB
        TrayState.DOWNLOADING: (40,  140, 220),
        TrayState.WARNING:     (220, 175,   0),
        TrayState.ERROR:       (210,  50,  50),
        TrayState.OFFLINE:     (145, 145, 145),
        TrayState.UPDATE:      (130,  80, 210),
    }[state]

    # 둥근 사각형 배경 (모든 상태 공통 — 형태 동일성)
    draw.rounded_rectangle([3, 3, 61, 61], radius=12, fill=bg)

    # ▶ 재생 삼각형 — 항상 표시 (앱 정체성)
    # 우측 뱃지가 있는 상태는 왼쪽으로 이동, 없는 상태는 가운데
    w = "white"
    has_badge = state not in (TrayState.IDLE, TrayState.OFFLINE)
    if has_badge:
        draw.polygon([(13, 16), (13, 48), (38, 32)], fill=w)
    else:
        draw.polygon([(18, 14), (18, 50), (48, 32)], fill=w)

    # 오른쪽 뱃지 영역 (x=44~58, y=14~49)
    f = frame % _ANIM_FRAMES
    if state == TrayState.DOWNLOADING:
        # ↓ 화살표가 아래로 흘러내림
        y_positions = [14, 21, 28, 35]
        alphas      = [255, 185, 110, 45]
        y = y_positions[f]
        c = (255, 255, 255, alphas[f])
        draw.rectangle([49, y,     55, y + 8],  fill=c)
        draw.polygon([(44, y + 8), (58, y + 8), (51, y + 14)], fill=c)

    elif state == TrayState.UPDATE:
        # ↑ 화살표가 위로 솟아오름
        y_positions = [35, 28, 21, 14]
        alphas      = [255, 185, 110, 45]
        y = y_positions[f]
        c = (255, 255, 255, alphas[f])
        draw.polygon([(44, y + 6), (58, y + 6), (51, y)], fill=c)
        draw.rectangle([49, y + 6, 55, y + 14], fill=c)

    elif state == TrayState.WARNING:
        draw.rectangle([49, 16, 55, 36], fill=w)
        draw.ellipse([49, 42, 55, 48],   fill=w)

    elif state == TrayState.ERROR:
        draw.line([43, 18, 59, 42], fill=w, width=5)
        draw.line([59, 18, 43, 42], fill=w, width=5)

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
    # frozen(설치된 exe): exe 직접 등록 / dev: VBS로 pythonw 무음 실행
    if getattr(sys, 'frozen', False):
        target = f'"{sys.executable}"'
    else:
        vbs    = os.path.join(config.BASE_DIR, "run_silent.vbs")
        target = f'wscript.exe "{vbs}"'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, _REG_NAME, 0, winreg.REG_SZ, target)
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
        self._update_info      = None   # {"version": ..., "url": ...} or None
        self._update_progress  = None   # None=대기/완료, int=다운로드 진행률(0~100)
        self._installer_path   = None   # 다운로드 완료된 인스톨러 경로
        self._update_dl_lock   = threading.Lock()   # 중복 다운로드 방지
        self._web_ok           = True   # 웹서버 정상 기동 여부
        self._unarchived_open  = False  # 관리 창 중복 열기 방지
        self._anim_stop       = threading.Event()
        self._anim_stop.set()          # 초기값: 정지 상태
        self._anim_thread     = None
        self._anim_state      = None   # 현재 애니 중인 TrayState

    # ── 상태 계산 ─────────────────────────────────────────────────────────────

    def _state(self) -> TrayState:
        with self._lock:
            if self._errors:
                return TrayState.ERROR
            if self._active_dl > 0:
                return TrayState.DOWNLOADING  # relay 오프라인 중에도 다운로드 표시
            if self._warnings:
                return TrayState.WARNING
        if not self._bot_connected:
            return TrayState.OFFLINE
        if self._update_info:
            return TrayState.UPDATE
        return TrayState.IDLE

    # ── 애니메이션 ────────────────────────────────────────────────────────────

    def _start_anim(self, state: TrayState):
        """애니메이션 루프 시작 (이전 루프 자동 정지)"""
        self._anim_stop.set()          # 기존 스레드 종료 신호
        stop = threading.Event()
        self._anim_stop  = stop
        self._anim_state = state

        def _loop():
            frame = 0
            while not stop.wait(_ANIM_INTERVAL):
                frame = (frame + 1) % _ANIM_FRAMES
                icon  = self._icon
                if icon and not stop.is_set():
                    try:
                        icon.icon = _make_icon(state, frame)
                    except Exception:
                        pass

        t = threading.Thread(target=_loop, daemon=True, name="tray-anim")
        self._anim_thread = t
        t.start()

    def _stop_anim(self):
        """애니메이션 루프 정지"""
        self._anim_stop.set()
        self._anim_state = None

    def _refresh(self):
        """아이콘·툴팁 즉시 갱신"""
        icon = self._icon
        if not icon:
            return
        s        = self._state()
        animated = s in (TrayState.DOWNLOADING, TrayState.UPDATE)
        try:
            if animated:
                if self._anim_state != s:
                    self._start_anim(s)
                # 아이콘 갱신은 애니메이션 스레드가 담당
            else:
                self._stop_anim()
                icon.icon = _make_icon(s)
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
        self._refresh()
        self.notify("StreamSaver 업데이트",
                    f"v{info['version']} 업데이트 발견 — 백그라운드에서 다운로드합니다.")
        # 사용자 액션 없이 즉시 백그라운드 다운로드 시작
        threading.Thread(target=self._run_auto_update, daemon=True,
                         name="AutoUpdater").start()

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
            TrayState.UPDATE:      "업데이트 있음",
        }[state]
        mode_label = "🌐 온라인" if self._mode == "relay" else "💻 로컬"
        rc = self.ctx.rc if self.ctx else None
        if self._mode == "relay" and rc and rc.connected:
            bot_s = "봇 ✅" if rc.bot_discord else "봇 ❌"
            lines = [f"StreamSaver  ·  {mode_label}  ·  {label}  ·  {bot_s}"]
        else:
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
        yield pystray.MenuItem("⚙️ 관리 창 열기...", self._manage_unarchived)
        yield pystray.Menu.SEPARATOR
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
        yield pystray.MenuItem("📁 다운로드 폴더 변경", self._change_download_folder)
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
            elif rc and (rc.guild_id or rc.has_saved_guild):
                mode_txt = "🔄 온라인 모드  —  재연결 중..."
            else:
                mode_txt = "🔌 온라인 모드  —  연결 필요"
        else:
            mode_txt = "💻 로컬 모드" + ("  —  연결됨" if self._bot_connected else "  —  미연결")
        yield pystray.MenuItem(mode_txt, None, enabled=False)

        if self._mode == "relay" and not (rc and rc.connected):
            if not (rc and rc.has_saved_guild):
                # 첫 설치 또는 리셋 — 봇 초대 + 서버 연결 모두 표시
                invite_url = getattr(config, "BOT_INVITE_URL", "")
                if invite_url:
                    yield pystray.MenuItem(
                        "🤖 1단계: Discord 봇 서버 초대",
                        lambda icon, item: self._open_url(invite_url),
                    )
                yield pystray.MenuItem("🔗 2단계: 서버 연결", self._connect_server)
            # has_saved_guild 있으면 자동 재연결 중 — 버튼 없음
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
                label = f"⬇️ 업데이트 다운로드 중 ({pct}%)"
                yield pystray.MenuItem(label, None, enabled=False)
            elif self._installer_path:
                yield pystray.MenuItem(
                    f"✅ v{v} 설치 준비됨  —  지금 설치",
                    self._do_install,
                )
            else:
                yield pystray.MenuItem(
                    f"⬆️ v{v} 업데이트 있음 (다운로드 재시도)",
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
                    rc = gui.ctx.rc if gui.ctx else None
                    if rc and rc.connected:
                        if rc.heartbeat_timeout:
                            gui.add_warning("relay_hang", "릴레이 서버 무응답 (90초 초과)")
                        else:
                            gui.clear_warning("relay_hang")
                        if not rc.bot_discord:
                            gui.add_warning("bot_discord", "Discord 봇 오프라인")
                        else:
                            gui.clear_warning("bot_discord")
                    else:
                        gui.clear_warning("relay_hang")
                        gui.clear_warning("bot_discord")
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
            from PIL import ImageTk

            # ── 색상 (dashboard 팔레트) ───────────────────────────
            BG      = "#ffffff"
            HEADER  = "#2563eb"
            PRIMARY = "#2563eb"
            P_HOV   = "#1d4ed8"
            SEC     = "#f3f4f6"
            S_HOV   = "#e5e7eb"
            TEXT    = "#111827"
            SUB     = "#6b7280"
            BORDER  = "#e5e7eb"

            root = tk.Tk()
            root.withdraw()

            dlg = tk.Toplevel(root)
            dlg.title("StreamSaver — 서버 연결")
            dlg.configure(bg=BG)
            dlg.resizable(False, False)
            dlg.attributes("-topmost", True)
            dlg.grab_set()

            W, H = 380, 430
            sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
            dlg.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")

            result = {"code": None}

            def _submit(e=None):
                result["code"] = entry.get().strip()
                dlg.destroy()

            def _cancel(e=None):
                dlg.destroy()

            dlg.protocol("WM_DELETE_WINDOW", _cancel)

            # ── 헤더 ─────────────────────────────────────────────
            hdr = tk.Frame(dlg, bg=HEADER, height=76)
            hdr.pack(fill="x")
            hdr.pack_propagate(False)

            icon_pil   = _make_icon(TrayState.IDLE).resize((44, 44))
            icon_photo = ImageTk.PhotoImage(icon_pil)
            ico_lbl = tk.Label(hdr, image=icon_photo, bg=HEADER)
            ico_lbl.image = icon_photo
            ico_lbl.pack(side="left", padx=(20, 12), pady=16)

            hdr_text = tk.Frame(hdr, bg=HEADER)
            hdr_text.pack(side="left")
            tk.Label(hdr_text, text="StreamSaver",
                     font=("Segoe UI", 13, "bold"), bg=HEADER, fg="white").pack(anchor="w")
            tk.Label(hdr_text, text="서버 연결",
                     font=("Segoe UI", 9), bg=HEADER, fg="#bfdbfe").pack(anchor="w")

            # ── 본문 ─────────────────────────────────────────────
            body = tk.Frame(dlg, bg=BG, padx=24, pady=16)
            body.pack(fill="both", expand=True)

            tk.Label(body,
                     text="Discord에서 /setup 을 입력하고 받은 코드를 입력하세요.",
                     font=("Segoe UI", 10), bg=BG, fg=TEXT,
                     wraplength=330, justify="left").pack(anchor="w")
            tk.Label(body,
                     text="아직 /setup을 실행하지 않았다면 먼저 Discord를 여세요.",
                     font=("Segoe UI", 10), bg=BG, fg=SUB,
                     wraplength=330, justify="left").pack(anchor="w", pady=(2, 16))

            # Discord 열기 버튼
            def _hover(w, lbl, on):
                c = S_HOV if on else SEC
                w.configure(bg=c)
                lbl.configure(bg=c)

            disc_f = tk.Frame(body, bg=BORDER, pady=1, padx=1)
            disc_f.pack(fill="x", pady=(0, 20))
            disc_i = tk.Frame(disc_f, bg=SEC, pady=9)
            disc_i.pack(fill="x")
            disc_l = tk.Label(disc_i, text="🔗   Discord 열기",
                              font=("Segoe UI", 10), bg=SEC, fg=TEXT, cursor="hand2")
            disc_l.pack()
            _cb_disc = lambda e: webbrowser.open("https://discord.com/app")
            for w in (disc_f, disc_i, disc_l):
                w.bind("<Button-1>", _cb_disc)
                w.bind("<Enter>", lambda e, i=disc_i, l=disc_l: _hover(i, l, True))
                w.bind("<Leave>", lambda e, i=disc_i, l=disc_l: _hover(i, l, False))

            # 입력 필드
            tk.Label(body, text="페어링 코드", font=("Segoe UI", 10, "bold"),
                     bg=BG, fg=TEXT).pack(anchor="w", pady=(0, 6))

            ef = tk.Frame(body, bg=BORDER, pady=1, padx=1)
            ef.pack(fill="x")
            entry = tk.Entry(ef, font=("Consolas", 16), justify="center",
                             bg="#f9fafb", fg=TEXT, insertbackground=TEXT,
                             relief="flat", bd=10)
            entry.pack(fill="x")
            entry.bind("<Return>", _submit)
            entry.focus_set()

            tk.Label(body, text="예: ABC-123", font=("Segoe UI", 9),
                     bg=BG, fg=SUB).pack(anchor="w", pady=(4, 0))

            # ── 하단 버튼 ─────────────────────────────────────────
            foot = tk.Frame(body, bg=BG)
            foot.pack(fill="x", pady=(16, 0))

            def _btn_hover(f, l, bg, hbg, on):
                c = hbg if on else bg
                f.configure(bg=c); l.configure(bg=c)

            # 취소
            can_f = tk.Frame(foot, bg=SEC, pady=9, padx=20)
            can_f.pack(side="right", padx=(8, 0))
            can_l = tk.Label(can_f, text="취소", font=("Segoe UI", 10),
                             bg=SEC, fg=TEXT, cursor="hand2")
            can_l.pack()
            for w in (can_f, can_l):
                w.bind("<Button-1>", _cancel)
                w.bind("<Enter>", lambda e: _btn_hover(can_f, can_l, SEC, S_HOV, True))
                w.bind("<Leave>", lambda e: _btn_hover(can_f, can_l, SEC, S_HOV, False))

            # 연결
            con_f = tk.Frame(foot, bg=PRIMARY, pady=9, padx=28)
            con_f.pack(side="right")
            con_l = tk.Label(con_f, text="연결", font=("Segoe UI", 10, "bold"),
                             bg=PRIMARY, fg="white", cursor="hand2")
            con_l.pack()
            for w in (con_f, con_l):
                w.bind("<Button-1>", _submit)
                w.bind("<Enter>", lambda e: _btn_hover(con_f, con_l, PRIMARY, P_HOV, True))
                w.bind("<Leave>", lambda e: _btn_hover(con_f, con_l, PRIMARY, P_HOV, False))

            root.wait_window(dlg)
            root.destroy()

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
        tmp_path = env_path + ".tmp"
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
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp_path, env_path)
        except Exception as e:
            logger.error("env update error: %s", e)
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    def _change_download_folder(self, icon=None, item=None):
        def _dialog():
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            new_dir = filedialog.askdirectory(
                title="다운로드 폴더 선택",
                initialdir=config.DOWNLOAD_DIR,
                parent=root,
            )
            root.destroy()
            if not new_dir:
                return
            new_dir = os.path.normpath(new_dir)
            config.DOWNLOAD_DIR = new_dir
            os.makedirs(new_dir, exist_ok=True)
            self._update_env_key("DOWNLOAD_DIR", new_dir)
            self.notify("StreamSaver", f"다운로드 폴더 변경됨:\n{new_dir}")
            self._refresh()
        threading.Thread(target=_dialog, daemon=True).start()

    def _manage_unarchived(self, icon=None, item=None):
        if self._unarchived_open:
            return
        self._unarchived_open = True

        def _dialog():
            try:
                _dialog_body()
            except Exception as e:
                logger.error("_manage_unarchived dialog error: %s", e, exc_info=True)
            finally:
                self._unarchived_open = False

        def _dialog_body():
            import tkinter as tk
            from tkinter import messagebox
            from stream_watcher import StreamWatcher as SW

            BG      = "#ffffff"
            HEADER  = "#2563eb"
            PRIMARY = "#2563eb"
            P_HOV   = "#1d4ed8"
            SEC     = "#f3f4f6"
            S_HOV   = "#e5e7eb"
            TEXT    = "#111827"
            SUB     = "#6b7280"
            BORDER  = "#e5e7eb"
            DELETE  = "#dc2626"
            D_HOV   = "#b91c1c"

            sw = self.ctx.sw if self.ctx else None

            root = tk.Tk()
            root.withdraw()

            dlg = tk.Toplevel(root)
            dlg.title("StreamSaver — Unarchived 관리")
            dlg.configure(bg=BG)
            dlg.resizable(False, False)
            dlg.attributes("-topmost", True)

            W, H = 420, 460
            scr_w = dlg.winfo_screenwidth()
            scr_h = dlg.winfo_screenheight()
            dlg.geometry(f"{W}x{H}+{(scr_w-W)//2}+{(scr_h-H)//2}")
            dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)

            # Header
            from PIL import ImageTk
            hdr = tk.Frame(dlg, bg=HEADER, height=76)
            hdr.pack(fill="x")
            hdr.pack_propagate(False)

            icon_pil   = _make_icon(TrayState.IDLE).resize((44, 44))
            icon_photo = ImageTk.PhotoImage(icon_pil)
            ico_lbl    = tk.Label(hdr, image=icon_photo, bg=HEADER)
            ico_lbl.image = icon_photo
            ico_lbl.pack(side="left", padx=(20, 12), pady=16)

            hdr_text = tk.Frame(hdr, bg=HEADER)
            hdr_text.pack(side="left")
            tk.Label(hdr_text, text="StreamSaver",
                     font=("Segoe UI", 13, "bold"), bg=HEADER, fg="white").pack(anchor="w")
            tk.Label(hdr_text, text="Unarchived 감지 관리",
                     font=("Segoe UI", 9), bg=HEADER, fg="#bfdbfe").pack(anchor="w")

            # Body
            body = tk.Frame(dlg, bg=BG, padx=24, pady=16)
            body.pack(fill="both", expand=True)

            tk.Label(body, text="감시 중인 채널",
                     font=("Segoe UI", 10, "bold"), bg=BG, fg=TEXT).pack(anchor="w", pady=(0, 6))

            list_frame = tk.Frame(body, bg=BORDER, pady=1, padx=1)
            list_frame.pack(fill="both", expand=True)
            list_inner = tk.Frame(list_frame, bg=BG)
            list_inner.pack(fill="both", expand=True)

            sb = tk.Scrollbar(list_inner)
            sb.pack(side="right", fill="y")
            listbox = tk.Listbox(
                list_inner,
                font=("Segoe UI", 10),
                bg=BG, fg=TEXT,
                selectbackground=PRIMARY, selectforeground="white",
                relief="flat", bd=0,
                yscrollcommand=sb.set,
                activestyle="none",
            )
            listbox.pack(fill="both", expand=True, padx=8, pady=8)
            sb.config(command=listbox.yview)

            _url_list = []

            def _refresh_list():
                listbox.delete(0, tk.END)
                _url_list.clear()
                channels = sw.list_channels() if sw else []
                for url, info in channels:
                    filt  = info["title_filter"] or "전체"
                    label = SW.display_name(info)
                    listbox.insert(tk.END, f"  {label}  [{filt}]")
                    _url_list.append(url)
                if not _url_list:
                    listbox.insert(tk.END, "  등록된 채널 없음")

            _refresh_list()

            def _btn_hover(f, l, bg, hbg, on):
                c = hbg if on else bg
                f.configure(bg=c)
                l.configure(bg=c)

            btn_row = tk.Frame(body, bg=BG)
            btn_row.pack(fill="x", pady=(10, 0))

            def _delete():
                sel = listbox.curselection()
                if not sel or sel[0] >= len(_url_list):
                    messagebox.showinfo("알림", "삭제할 채널을 선택하세요.", parent=dlg)
                    return
                url = _url_list[sel[0]]
                if sw:
                    sw.remove(url)
                    self._refresh()
                _refresh_list()

            del_f = tk.Frame(btn_row, bg=DELETE, pady=8, padx=16)
            del_f.pack(side="right", padx=(6, 0))
            del_l = tk.Label(del_f, text="삭제", font=("Segoe UI", 10, "bold"),
                             bg=DELETE, fg="white", cursor="hand2")
            del_l.pack()
            for w in (del_f, del_l):
                w.bind("<Button-1>", lambda e: _delete())
                w.bind("<Enter>",   lambda e: _btn_hover(del_f, del_l, DELETE, D_HOV, True))
                w.bind("<Leave>",   lambda e: _btn_hover(del_f, del_l, DELETE, D_HOV, False))

            def _add():
                sub = tk.Toplevel(dlg)
                sub.title("채널 추가")
                sub.configure(bg=BG)
                sub.resizable(False, False)
                sub.attributes("-topmost", True)
                sub.grab_set()

                W2, H2 = 360, 275
                sub.geometry(f"{W2}x{H2}+{(scr_w-W2)//2}+{(scr_h-H2)//2}")
                sub.protocol("WM_DELETE_WINDOW", sub.destroy)

                body2 = tk.Frame(sub, bg=BG, padx=24, pady=18)
                body2.pack(fill="both", expand=True)

                def _field(label_text, default=""):
                    tk.Label(body2, text=label_text,
                             font=("Segoe UI", 10, "bold"), bg=BG, fg=TEXT).pack(
                                 anchor="w", pady=(8, 2))
                    ef = tk.Frame(body2, bg=BORDER, pady=1, padx=1)
                    ef.pack(fill="x")
                    e = tk.Entry(ef, font=("Segoe UI", 10), bg="#f9fafb", fg=TEXT,
                                 insertbackground=TEXT, relief="flat", bd=8)
                    e.insert(0, default)
                    e.pack(fill="x")
                    return e

                url_e    = _field("YouTube 채널 URL *")
                name_e   = _field("표시 이름 (선택)")
                filter_e = _field("필터 키워드", "unarchived")
                url_e.focus_set()

                def _confirm(e=None):
                    url = url_e.get().strip()
                    if not url.startswith(("http://", "https://")):
                        messagebox.showwarning("입력 오류",
                                               "올바른 YouTube URL을 입력하세요.", parent=sub)
                        return
                    name = name_e.get().strip()
                    filt = filter_e.get().strip()
                    if sw:
                        sw.add(url, name, filt)
                        self._refresh()
                    sub.destroy()
                    _refresh_list()

                url_e.bind("<Return>", _confirm)
                sub.protocol("WM_DELETE_WINDOW", sub.destroy)

                foot_s = tk.Frame(body2, bg=BG)
                foot_s.pack(fill="x", pady=(16, 0))

                can_f2 = tk.Frame(foot_s, bg=SEC, pady=8, padx=16)
                can_f2.pack(side="right", padx=(6, 0))
                can_l2 = tk.Label(can_f2, text="취소", font=("Segoe UI", 10),
                                  bg=SEC, fg=TEXT, cursor="hand2")
                can_l2.pack()
                for w in (can_f2, can_l2):
                    w.bind("<Button-1>", lambda e: sub.destroy())
                    w.bind("<Enter>",   lambda e: _btn_hover(can_f2, can_l2, SEC, S_HOV, True))
                    w.bind("<Leave>",   lambda e: _btn_hover(can_f2, can_l2, SEC, S_HOV, False))

                add_f2 = tk.Frame(foot_s, bg=PRIMARY, pady=8, padx=16)
                add_f2.pack(side="right")
                add_l2 = tk.Label(add_f2, text="추가", font=("Segoe UI", 10, "bold"),
                                  bg=PRIMARY, fg="white", cursor="hand2")
                add_l2.pack()
                for w in (add_f2, add_l2):
                    w.bind("<Button-1>", lambda e: _confirm())
                    w.bind("<Enter>",   lambda e: _btn_hover(add_f2, add_l2, PRIMARY, P_HOV, True))
                    w.bind("<Leave>",   lambda e: _btn_hover(add_f2, add_l2, PRIMARY, P_HOV, False))

                dlg.wait_window(sub)

            add_f = tk.Frame(btn_row, bg=PRIMARY, pady=8, padx=16)
            add_f.pack(side="right")
            add_l = tk.Label(add_f, text="+ 채널 추가", font=("Segoe UI", 10, "bold"),
                             bg=PRIMARY, fg="white", cursor="hand2")
            add_l.pack()
            for w in (add_f, add_l):
                w.bind("<Button-1>", lambda e: _add())
                w.bind("<Enter>",   lambda e: _btn_hover(add_f, add_l, PRIMARY, P_HOV, True))
                w.bind("<Leave>",   lambda e: _btn_hover(add_f, add_l, PRIMARY, P_HOV, False))

            tk.Label(body, text="* 변경사항은 즉시 반영됩니다",
                     font=("Segoe UI", 9), bg=BG, fg=SUB).pack(anchor="w", pady=(8, 0))

            # Footer
            tk.Frame(dlg, bg=BORDER, height=1).pack(fill="x")
            foot = tk.Frame(dlg, bg=BG, padx=24, pady=12)
            foot.pack(fill="x")

            close_f = tk.Frame(foot, bg=SEC, pady=8, padx=20)
            close_f.pack(side="right")
            close_l = tk.Label(close_f, text="닫기", font=("Segoe UI", 10),
                               bg=SEC, fg=TEXT, cursor="hand2")
            close_l.pack()
            for w in (close_f, close_l):
                w.bind("<Button-1>", lambda e: dlg.destroy())
                w.bind("<Enter>",   lambda e: _btn_hover(close_f, close_l, SEC, S_HOV, True))
                w.bind("<Leave>",   lambda e: _btn_hover(close_f, close_l, SEC, S_HOV, False))

            root.wait_window(dlg)
            root.destroy()

        threading.Thread(target=_dialog, daemon=True).start()

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
            if getattr(sys, "frozen", False):
                cmd = [sys.executable]
            else:
                cmd = [sys.executable, os.path.join(config.BASE_DIR, "main.py")]
            subprocess.Popen(cmd, creationflags=flags, cwd=config.BASE_DIR)
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

    def _start_auto_update(self, icon=None, item=None):
        """다운로드 실패 후 재시도 또는 수동 트리거"""
        if self._update_progress is not None or self._installer_path:
            return
        threading.Thread(target=self._run_auto_update, daemon=True,
                         name="AutoUpdater").start()

    def _run_auto_update(self):
        import updater
        info = self._update_info
        if not info:
            return
        with self._update_dl_lock:
            if self._update_progress is not None or self._installer_path:
                return  # 이미 진행 중이거나 완료됨
            self._update_progress = 0  # Lock 안에서 선점

        url = info["url"]
        _last_pct = [0]

        def on_progress(pct: int):
            self._update_progress = pct
            if pct - _last_pct[0] >= 10 or pct == 100:
                _last_pct[0] = pct
                self._refresh()

        try:
            self._refresh()
            installer_path = updater.download_update(url, on_progress)
            self._installer_path = installer_path
            self._update_progress = None
            self._refresh()
            v = info["version"]
            self.notify("StreamSaver 업데이트 준비됨",
                        f"v{v} 다운로드 완료 — 트레이 메뉴에서 지금 설치하세요.")
        except Exception as e:
            logger.error("Auto-update download failed: %s", e)
            self._update_progress = None
            self._refresh()
            self.notify("StreamSaver", f"업데이트 다운로드 실패: {e}")

    def _do_install(self, icon=None, item=None):
        """다운로드 완료된 인스톨러를 /VERYSILENT로 즉시 실행 — 다이얼로그 없음."""
        installer = self._installer_path
        if not installer:
            return
        if not os.path.exists(installer):
            self.notify("StreamSaver", "설치 파일을 찾을 수 없습니다. 다시 시도해 주세요.")
            self._installer_path = None
            self._refresh()
            return
        def _run():
            try:
                import updater
                if self.ctx:
                    self.ctx.cleanup()
                if self._icon:
                    try:
                        self._icon.stop()
                    except Exception:
                        pass
                updater.install_update(installer)
                os._exit(0)
            except Exception as e:
                logger.error("Install failed: %s", e)
                self.notify("StreamSaver", f"설치 실패: {e}")
        threading.Thread(target=_run, daemon=True, name="Installer").start()

    def _on_quit(self, icon, item=None):
        logger.info("Tray: quit")
        self._running = False
        icon.stop()
        if self.ctx:
            self.ctx.cleanup()
        os._exit(0)
