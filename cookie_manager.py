"""
CookieManager — Edge CDP 기반 YouTube 쿠키 추출기

핵심 원칙:
  - Edge 실행 전에 모든 msedge.exe 프로세스를 완전히 종료
  - CDP 포트를 netstat으로 실제 LISTENING 여부까지 확인
  - 실패 시 원인을 명확히 로그로 출력
"""

import os
import json
import time
import logging
import socket
import subprocess
import threading
import urllib.request
import platform
from enum import Enum, auto

import config

logger = logging.getLogger("StreamSaver.CookieManager")
_NW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)  # CMD 창 숨김

IS_WINDOWS = platform.system() == "Windows"

EDGE_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-extensions",
    "--hide-crash-restore-bubble",
    "--disable-features=msAppBoundEncryption,Translate",
    "--disable-background-networking",
]

LOCK_FILE = os.path.join(config.BASE_DIR, "bot_edge.lock")

_YT_AUTH_COOKIES = {"__Secure-3PAPISID", "SAPISID", "__Secure-1PAPISID", "SID"}


class EdgeState(Enum):
    STOPPED  = auto()
    HEADLESS = auto()
    VISIBLE  = auto()


# ── 락 파일 ─────────────────────────────────────────────────────────────────

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
        logger.warning("lock write failed: %s", e)


def _clear_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


# ── Edge 프로세스 전체 정리 ──────────────────────────────────────────────────

def kill_all_edge():
    """
    bot_profile을 쓰는 Edge를 포함해 관련 Edge 프로세스를 모두 종료.
    새 Edge를 시작하기 전에 반드시 호출한다.
    """
    if not IS_WINDOWS:
        return

    profile = os.path.normcase(config.BOT_EDGE_PROFILE)
    to_kill = []

    # PowerShell로 msedge.exe 프로세스의 CommandLine 조회
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-WmiObject Win32_Process -Filter \"Name='msedge.exe'\" "
             "| Select-Object @{N='PID';E={$_.ProcessId}},@{N='CMD';E={$_.CommandLine}} "
             "| ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=15, creationflags=_NW)

        if r.stdout.strip():
            data = json.loads(r.stdout)
            if isinstance(data, dict):
                data = [data]
            for proc in data:
                pid = proc.get("PID")
                cmd = proc.get("CMD") or ""
                if pid and profile in os.path.normcase(cmd):
                    to_kill.append(int(pid))
    except Exception as e:
        logger.warning("[EDGE_KILL] PowerShell scan failed: %s", e)

    # lock 파일 PID도 포함
    lock_pid, _ = _read_lock()
    if lock_pid and lock_pid not in to_kill:
        to_kill.append(lock_pid)

    for p in to_kill:
        try:
            subprocess.run(["taskkill", "/pid", str(p), "/f"],
                           capture_output=True, timeout=5, creationflags=_NW)
            logger.info("[EDGE_KILL] Killed PID %d", p)
        except Exception:
            pass

    if to_kill:
        time.sleep(1.5)

    _clear_lock()


# ── CookieManager ────────────────────────────────────────────────────────────

class CookieManager:
    def __init__(self):
        self.cookie_valid = False
        self._state = EdgeState.STOPPED
        self._edge_pid = None
        self._cdp_port = None
        self._state_lock = threading.Lock()
        self._login_lock = threading.Lock()
        self._cookie_lock = threading.Lock()
        self._on_status_change = None
        self._running = False

    @property
    def edge_pid(self):
        return self._edge_pid

    @property
    def cdp_port(self):
        return self._cdp_port

    def on_status_change(self, callback):
        self._on_status_change = callback

    # ── 프로세스 종료 ──────────────────────────────────────────────────────
    def _kill_edge(self):
        with self._state_lock:
            pid  = self._edge_pid
            port = self._cdp_port
            self._edge_pid = None
            self._cdp_port = None
            self._state = EdgeState.STOPPED

        if pid and IS_WINDOWS:
            try:
                subprocess.run(["taskkill", "/pid", str(pid), "/f"],
                               capture_output=True, timeout=5, creationflags=_NW)
                time.sleep(0.5)
                logger.info("[KILL] PID %d", pid)
            except Exception as e:
                logger.warning("[KILL] PID %d failed: %s", pid, e)

        if port and IS_WINDOWS:
            try:
                r = subprocess.run(
                    f"netstat -ano | findstr LISTENING | findstr \":{port} \"",
                    capture_output=True, text=True, shell=True, timeout=5,
                    creationflags=_NW)
                for line in r.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        try:
                            subprocess.run(
                                ["taskkill", "/pid", parts[-1], "/f"],
                                capture_output=True, timeout=5, creationflags=_NW)
                            logger.info("[KILL] port %d PID %s", port, parts[-1])
                        except Exception:
                            pass
            except Exception:
                pass

    def _kill_pid(self, pid):
        if not pid or not IS_WINDOWS:
            return
        try:
            subprocess.run(["taskkill", "/pid", str(pid), "/f"],
                           capture_output=True, timeout=5, creationflags=_NW)
        except Exception:
            pass

    # ── CDP 연결 확인 ──────────────────────────────────────────────────────
    def _is_running(self):
        """CDP HTTP 엔드포인트가 응답하는지 확인 (127.0.0.1 / localhost 모두)"""
        port = self._cdp_port
        if not port:
            return False
        for host in ("127.0.0.1", "localhost"):
            try:
                with urllib.request.urlopen(
                    f"http://{host}:{port}/json/version", timeout=2
                ) as resp:
                    json.loads(resp.read().decode())
                    return True
            except Exception:
                continue
        return False

    def _port_listening(self, port):
        """netstat으로 포트가 실제로 LISTENING 중인지 확인"""
        if not IS_WINDOWS or not port:
            return None  # 확인 불가
        try:
            r = subprocess.run(
                f"netstat -ano | findstr \":{port} \" | findstr LISTENING",
                capture_output=True, text=True, shell=True, timeout=4)
            return bool(r.stdout.strip())
        except Exception:
            return None

    # ── 프로필 세션 파일 정리 ──────────────────────────────────────────────
    def _cleanup_profile_session(self):
        """강제 종료 후 남는 세션 복원 파일 삭제 (쿠키는 보존)"""
        profile = config.BOT_EDGE_PROFILE
        targets = [
            "lockfile",
            os.path.join("Default", "Last Session"),
            os.path.join("Default", "Last Tabs"),
            os.path.join("Default", "Current Session"),
            os.path.join("Default", "Current Tabs"),
        ]
        for rel in targets:
            p = os.path.join(profile, rel)
            if os.path.exists(p):
                try:
                    os.remove(p)
                    logger.debug("[PROFILE] removed %s", rel)
                except Exception:
                    pass

    # ── headless 시작 ──────────────────────────────────────────────────────
    def start_headless(self):
        if not IS_WINDOWS:
            return False
        if not os.path.exists(config.EDGE_PATH):
            logger.error("[HEADLESS] Edge not found: %s", config.EDGE_PATH)
            return False

        # 기존 Edge 완전 종료
        kill_all_edge()
        self._cleanup_profile_session()
        time.sleep(1.0)

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
                "about:blank",
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0)

            with self._state_lock:
                self._edge_pid = proc.pid
                self._cdp_port = port
                self._state = EdgeState.HEADLESS
            _write_lock(proc.pid, port)
            logger.info("[HEADLESS] PID=%d port=%d", proc.pid, port)

            # CDP 대기 (최대 15s)
            for attempt in range(15):
                time.sleep(1)
                if self._is_running():
                    logger.info("[HEADLESS] CDP ready after %ds", attempt + 1)
                    return True
                # 5s 후 포트 LISTENING 여부로 진단
                if attempt == 5:
                    listening = self._port_listening(port)
                    if listening is False:
                        logger.warning(
                            "[HEADLESS] Port %d not LISTENING at 5s — "
                            "Edge may have failed to bind (profile locked?)", port)

            logger.warning("[HEADLESS] CDP not ready after 15s — "
                          "port %s listening=%s",
                          port, self._port_listening(port))
            return False
        except Exception as e:
            logger.error("[HEADLESS] Failed to start: %s", e)
            with self._state_lock:
                self._edge_pid = None
                self._cdp_port = None
                self._state = EdgeState.STOPPED
            _clear_lock()
            return False

    # ── visible 시작 (로그인용) ─────────────────────────────────────────────
    def start_visible(self, cdp_timeout=45):
        if not IS_WINDOWS:
            return False

        # 기존 Edge 완전 종료 (personal Edge 포함 봇 프로필 사용 Edge)
        kill_all_edge()
        self._cleanup_profile_session()
        time.sleep(2.0)

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
                # 개인 Edge와 다른 창으로 확실히 분리
                "--new-window",
                "https://www.youtube.com",
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_NW)

            with self._state_lock:
                self._edge_pid = proc.pid
                self._cdp_port = port
                self._state = EdgeState.VISIBLE
            _write_lock(proc.pid, port)
            logger.info("[VISIBLE] PID=%d port=%d", proc.pid, port)

            for attempt in range(cdp_timeout):
                time.sleep(1)
                if self._is_running():
                    logger.info("[VISIBLE] CDP ready after %ds", attempt + 1)
                    return True
                if attempt == 10:
                    listening = self._port_listening(port)
                    logger.info("[VISIBLE] 10s check — port %d listening=%s",
                                port, listening)

            logger.warning("[VISIBLE] CDP not ready after %ds "
                          "(port %d listening=%s)",
                          cdp_timeout, port, self._port_listening(port))
            return False
        except Exception as e:
            logger.error("[VISIBLE] Failed: %s", e)
            with self._state_lock:
                self._edge_pid = None
                self._cdp_port = None
                self._state = EdgeState.STOPPED
            _clear_lock()
            return False

    # ── CDP helpers ────────────────────────────────────────────────────────
    def _cdp_request(self, path):
        port = self._cdp_port
        if not port:
            return None
        for host in ("127.0.0.1", "localhost"):
            try:
                with urllib.request.urlopen(
                    f"http://{host}:{port}{path}", timeout=5
                ) as resp:
                    return json.loads(resp.read().decode())
            except Exception:
                continue
        return None

    def _cdp_page_ws_url(self):
        """페이지 타겟 WebSocket URL — Network.getAllCookies는 page target에서만 동작"""
        port = self._cdp_port
        if not port:
            return None
        for host in ("127.0.0.1", "localhost"):
            try:
                with urllib.request.urlopen(
                    f"http://{host}:{port}/json/list", timeout=5
                ) as resp:
                    targets = json.loads(resp.read().decode())
                    for t in targets:
                        if t.get("type") == "page":
                            ws = t.get("webSocketDebuggerUrl", "")
                            if ws:
                                logger.debug("[CDP] page target: %s", ws[:60])
                                return ws
            except Exception:
                continue
        return None

    def _cdp_browser_ws_url(self):
        """브라우저 타겟 WebSocket URL (fallback)"""
        data = self._cdp_request("/json/version")
        return data.get("webSocketDebuggerUrl") if data else None

    def _cdp_get_all_cookies(self):
        import websocket

        ws_url = None
        for attempt in range(5):
            # 페이지 타겟 우선 — Network.getAllCookies가 여기서만 쿠키 반환
            ws_url = self._cdp_page_ws_url() or self._cdp_browser_ws_url()
            if ws_url:
                break
            time.sleep(1.5 * (attempt + 1))

        if not ws_url:
            logger.warning("[CDP] WS URL unavailable")
            return None

        target_type = "page" if "/page/" in ws_url else "browser"
        logger.info("[CDP] target: %s | url: %s", target_type, ws_url[:60])

        for origin in (None, f"http://127.0.0.1:{self._cdp_port}"):
            try:
                kw = {"timeout": 10}
                if origin is not None:
                    kw["origin"] = origin
                ws = websocket.create_connection(ws_url, **kw)
                ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
                resp = ws.recv()
                ws.close()
                cookies = json.loads(resp).get("result", {}).get("cookies", [])
                logger.info("[CDP] getAllCookies → %d cookies (via %s target)",
                            len(cookies), target_type)
                return cookies
            except Exception as e:
                if "403" in str(e):
                    logger.debug("[CDP] 403 on origin=%s, retrying", origin)
                    continue
                logger.warning("[CDP] getAllCookies error: %s", e)
                return None
        return None

    @staticmethod
    def _has_yt_session(cookies):
        names = {c.get("name", "") for c in cookies}
        return bool(names & _YT_AUTH_COOKIES)

    def _cookies_to_netscape(self, cookies):
        lines = ["# Netscape HTTP Cookie File"]
        for c in cookies:
            domain = c.get("domain", "")
            if not domain:
                continue
            if not domain.startswith("."):
                domain = "." + domain

            path   = c.get("path") or "/"
            secure = "TRUE" if c.get("secure") else "FALSE"

            # CDP session cookies have expires=-1; Netscape format uses 0 for session cookies
            raw_exp = c.get("expires", 0)
            if raw_exp is None or raw_exp < 0:
                expires = 0
            else:
                expires = int(raw_exp)

            name  = c.get("name", "")
            value = str(c.get("value", "")).replace("\t", "").replace("\n", "").replace("\r", "")
            if not name:
                continue

            lines.append(f"{domain}\tTRUE\t{path}\t{secure}\t{expires}\t{name}\t{value}")
        return "\n".join(lines) + "\n"

    # ── 쿠키 추출 ──────────────────────────────────────────────────────────
    def extract_cookies(self):
        with self._cookie_lock:
            if not self._is_running():
                logger.warning("[COOKIE] CDP not available")
                return False

            cookies = self._cdp_get_all_cookies()
            if not cookies:
                logger.warning("[COOKIE] 0 cookies from CDP")
                self.cookie_valid = False
                return False

            yt = [c for c in cookies
                  if any(d in c.get("domain", "")
                         for d in [".youtube", "youtube.com",
                                   ".google", "google.com",
                                   "accounts.google"])]
            target = yt if yt else cookies
            if not yt:
                logger.info("[COOKIE] No YT cookies, saving all %d", len(cookies))

            tmp_cookie = config.COOKIE_FILE + ".tmp"
            with open(tmp_cookie, "w", encoding="utf-8") as f:
                f.write(self._cookies_to_netscape(target))
            os.replace(tmp_cookie, config.COOKIE_FILE)

            sz = os.path.getsize(config.COOKIE_FILE)
            self.cookie_valid = sz > 200
            logger.info("[COOKIE] %d total / %d YT / %d bytes / valid=%s",
                        len(cookies), len(yt), sz, self.cookie_valid)
            return self.cookie_valid

    def refresh_cookies(self):
        logger.info("Cookie refresh...")
        ok = self.extract_cookies()
        logger.info("Cookie refresh %s", "OK" if ok else "FAILED")
        if self._on_status_change:
            self._on_status_change("cookie_refresh", ok)
        return ok

    def start_auto_refresh(self):
        self._running = True

        def _loop():
            _last_restart = 0.0
            while self._running:
                time.sleep(config.COOKIE_REFRESH_INTERVAL)
                if not self._running:
                    break
                # Edge CDP 무응답 시 자동 복구 (로그인 중 제외, 5분 쿨다운)
                if (not self._is_running()
                        and not self._login_lock.locked()
                        and time.time() - _last_restart > 300):
                    logger.warning("[AUTO] CDP not responding — restarting Edge")
                    _last_restart = time.time()
                    self._restart_headless()
                self.refresh_cookies()

        threading.Thread(target=_loop, daemon=True).start()
        logger.info("Auto cookie refresh every %ds", config.COOKIE_REFRESH_INTERVAL)

    # ── 로그인 플로우 ──────────────────────────────────────────────────────
    def login_flow(self, on_done=None, on_progress=None):
        if not IS_WINDOWS:
            if on_done:
                on_done(False)
            return

        if not self._login_lock.acquire(blocking=False):
            logger.warning("[LOGIN] Already in progress")
            if on_progress:
                on_progress("⚠️ 이미 로그인 진행 중입니다")
            if on_done:
                on_done(False)
            return

        def _run():
            try:
                self._do_login_flow(on_done, on_progress)
            finally:
                self._login_lock.release()

        threading.Thread(target=_run, daemon=True).start()

    def _do_login_flow(self, on_done, on_progress):
        def notify(msg):
            logger.info("[LOGIN] %s", msg)
            if on_progress:
                on_progress(msg)

        # ① visible Edge 시작
        notify("🔄 모든 Edge 종료 → 세션 파일 정리 → visible Edge 재시작...")
        self.start_visible(cdp_timeout=45)

        if not self._is_running():
            listening = self._port_listening(self._cdp_port)
            notify(
                f"❌ Edge CDP 45초 대기 후 응답 없음\n"
                f"포트 {self._cdp_port} LISTENING: {listening}\n"
                "Edge가 실행됐으나 CDP가 비활성화됐거나, "
                "개인 Edge가 URL을 가로챘을 수 있습니다.\n"
                "**`!에지디버그` 명령어로 상세 진단을 확인하세요.**")
            self._restart_headless()
            if on_done:
                on_done(False)
            return

        notify("✅ Edge CDP 연결됨 — 쿠키 확인 중...")

        # ② 이미 로그인 상태인지 즉시 확인
        cookies = self._cdp_get_all_cookies()
        total = len(cookies) if cookies else 0
        yt_ck = [c for c in (cookies or [])
                 if any(d in c.get("domain", "")
                        for d in [".youtube", ".google"])]
        notify(f"현재 쿠키: 전체 {total}개 / YouTube·Google {len(yt_ck)}개")

        if cookies and self._has_yt_session(cookies):
            notify("✅ 이미 로그인 상태 감지! 쿠키 저장 중...")
            self.extract_cookies()
            self._restart_headless()
            if on_done:
                on_done(self.cookie_valid)
            return

        # ③ 로그인 대기 (5분)
        notify("YouTube에 로그인해 주세요. 5분 대기...")

        deadline = time.time() + 300
        logged_in = False
        cdp_gone = 0
        last_status = time.time()

        while time.time() < deadline:
            if not self._is_running():
                cdp_gone += 1
                if cdp_gone >= 5:
                    notify("Edge 창이 닫혔습니다")
                    break
                time.sleep(1)
                continue
            cdp_gone = 0

            cookies = self._cdp_get_all_cookies()
            if cookies and self._has_yt_session(cookies):
                notify(f"✅ YouTube 로그인 감지 ({len(cookies)}개 쿠키)")
                logged_in = True
                break

            if time.time() - last_status >= 30:
                remaining = int(deadline - time.time())
                yt = [c for c in (cookies or [])
                      if any(d in c.get("domain", "")
                             for d in [".youtube", ".google"])]
                notify(f"대기 중 — 쿠키 {len(cookies) if cookies else 0}개 "
                       f"(YouTube: {len(yt)}개) | 남은 {remaining}초")
                last_status = time.time()

            time.sleep(2)
        else:
            notify("⏰ 5분 초과")

        if self._is_running():
            self.extract_cookies()

        self._restart_headless()
        if on_done:
            on_done(self.cookie_valid)

    def _restart_headless(self):
        self._kill_edge()
        time.sleep(1.0)
        self.start_headless()

    # ── 진단 ──────────────────────────────────────────────────────────────
    def test_cdp(self):
        """!쿠키확인 용 — CDP 연결 및 쿠키 상태"""
        port = self._cdp_port
        listening = self._port_listening(port)
        running = self._is_running()

        if not running:
            return (
                f"❌ Edge CDP 응답 없음\n"
                f"상태: {self._state.name} | 포트: {port}\n"
                f"포트 LISTENING: {listening}\n"
                f"→ {'포트는 열려 있으나 HTTP 응답 없음' if listening else '포트 자체가 열리지 않음 (Edge 미실행)'}"
            )

        cookies = self._cdp_get_all_cookies()
        if cookies is None:
            return "❌ CDP getAllCookies 실패 (WebSocket 오류)"

        yt = [c for c in cookies
              if any(d in c.get("domain", "")
                     for d in [".youtube", ".google", "accounts.google"])]
        has_session = self._has_yt_session(cookies)
        yt_names = [c.get("name", "") for c in yt[:15]]

        return (
            f"**CDP 쿠키 현황**\n"
            f"Edge: {self._state.name} | 포트: {port}\n"
            f"전체: {len(cookies)}개 | YouTube·Google: {len(yt)}개\n"
            f"로그인: {'✅' if has_session else '❌'}\n"
            f"쿠키 이름: `{yt_names}`"
        )

    def debug_edge(self):
        """!에지디버그 용 — Edge 프로세스 전체 진단"""
        lines = [f"**Edge 진단 리포트**", f"상태: {self._state.name} | 포트: {self._cdp_port}"]

        # 실행 중인 msedge.exe 프로세스 목록
        try:
            r = subprocess.run(
                'tasklist /fi "imagename eq msedge.exe" /fo csv /nh',
                capture_output=True, text=True, shell=True, timeout=5,
                creationflags=_NW)
            pids = []
            for line in r.stdout.splitlines():
                parts = line.strip('"').split('","')
                if len(parts) >= 2:
                    try:
                        pids.append(int(parts[1]))
                    except ValueError:
                        pass
            lines.append(f"msedge.exe 프로세스: {len(pids)}개 (PIDs: {pids[:10]})")
        except Exception as e:
            lines.append(f"프로세스 목록 실패: {e}")

        # CDP 포트 상태
        port = self._cdp_port
        if port:
            listening = self._port_listening(port)
            lines.append(f"포트 {port} LISTENING: {listening}")
            running = self._is_running()
            lines.append(f"CDP HTTP 응답: {running}")

        # 프로필 존재 여부
        profile = config.BOT_EDGE_PROFILE
        lines.append(f"프로필 경로: {profile}")
        lines.append(f"프로필 존재: {os.path.exists(profile)}")

        # 쿠키 파일
        ck = config.COOKIE_FILE
        if os.path.exists(ck):
            lines.append(f"cookie.txt: {os.path.getsize(ck)} bytes")
        else:
            lines.append("cookie.txt: 없음")

        return "\n".join(lines)

    # ── 쿠키 만료 확인 ────────────────────────────────────────────────────
    def cookie_days_remaining(self) -> int | None:
        """인증 쿠키 중 가장 빨리 만료되는 것까지 남은 일 수.
        세션 쿠키만 있거나 파일 없으면 None."""
        if not os.path.exists(config.COOKIE_FILE):
            return None
        try:
            soonest = None
            with open(config.COOKIE_FILE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) < 7:
                        continue
                    name = parts[5]
                    if name not in _YT_AUTH_COOKIES:
                        continue
                    try:
                        exp = int(parts[4])
                    except ValueError:
                        continue
                    if exp <= 0:
                        continue  # 세션 쿠키 — 만료 없음
                    if soonest is None or exp < soonest:
                        soonest = exp
            if soonest is None:
                return None
            return max(0, int((soonest - time.time()) / 86400))
        except Exception:
            return None

    # ── lifecycle ──────────────────────────────────────────────────────────
    def stop(self):
        self._running = False
        self._kill_edge()
        _clear_lock()

    def get_status(self):
        f_exists = os.path.exists(config.COOKIE_FILE)
        f_size = os.path.getsize(config.COOKIE_FILE) if f_exists else 0
        return {
            "cookie_valid": self.cookie_valid,
            "cookie_file":  f_exists and f_size > 100,
            "cookie_size":  f_size,
            "platform":     "windows" if IS_WINDOWS else "linux",
            "edge_running": self._is_running(),
            "edge_state":   self._state.name,
        }
