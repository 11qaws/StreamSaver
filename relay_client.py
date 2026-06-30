"""
StreamSaver Relay Client
Windows PC에서 실행 — relay_server와 WebSocket으로 연결
Discord 명령을 받아서 로컬에서 실행하고 결과를 반환
"""
import asyncio
import json
import logging
import os
import socket
import threading
import time

import websockets

import config

logger = logging.getLogger("StreamSaver.RelayClient")

RECONNECT_DELAY = 10   # 재연결 대기 초


def _set_keepalive(ws):
    """TCP keepalive 설정 — NAT 테이블 유지용."""
    try:
        sock = ws.transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except Exception as e:
        logger.debug("keepalive setup failed: %s", e)


class RelayClient:
    def __init__(self, download_manager, cookie_manager, stream_watcher):
        self.dl  = download_manager
        self.cm  = cookie_manager
        self.sw  = stream_watcher
        self._ws         = None
        self._guild_id   = None
        self._pair_code  = None     # 연결 전: 페어링 코드
        self._connected  = False
        self._bot_discord    = False   # Discord 봇 온라인 여부 (릴레이 push)
        self._last_heartbeat = 0.0     # 마지막 heartbeat 수신 시각 (0=미수신)
        self._task       = None
        self._loop       = None
        self._on_connect_cb        = None
        self._on_disconnect_cb     = None
        self._on_watcher_change_cb = None
        self._on_error_cb          = None

        # 다운로드 이벤트 → relay로 전달
        self.dl.on_event(self._on_dl_event)

    # ── 외부 API ──────────────────────────────────────────────────────────────

    def set_pair_code(self, code: str):
        """트레이 UI에서 페어링 코드 입력 시 호출"""
        self._pair_code = code.strip().upper()
        logger.info("Pair code set: %s", self._pair_code)

    def on_connect(self, cb):
        self._on_connect_cb = cb

    def on_disconnect(self, cb):
        self._on_disconnect_cb = cb

    def on_watcher_change(self, cb):
        self._on_watcher_change_cb = cb

    def on_error(self, cb):
        self._on_error_cb = cb

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def needs_pair_code(self) -> bool:
        """저장된 guild_id도 없고 pair_code도 없어서 사용자 입력이 필요한 상태"""
        return not self._connected and not self._load_guild_id() and not self._pair_code

    @property
    def guild_id(self):
        return self._guild_id

    @property
    def has_saved_guild(self) -> bool:
        """파일 기반 체크 — 시작 직후에도 재연결 여부 즉시 판단 가능"""
        return bool(self._load_guild_id())

    @property
    def bot_discord(self) -> bool:
        """릴레이 서버가 push한 Discord 봇 온라인 여부"""
        return self._bot_discord

    @property
    def heartbeat_timeout(self) -> bool:
        """connected 상태에서 90초 이상 heartbeat 없으면 릴레이 hang 의심"""
        if not self._connected or self._last_heartbeat == 0.0:
            return False
        return time.time() - self._last_heartbeat > 90

    def start(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._task = loop.create_task(self._run_forever())
        logger.info("RelayClient started (server=%s)", config.RELAY_SERVER_URL)

    def stop(self):
        if self._task:
            self._task.cancel()

    # ── 연결 루프 ─────────────────────────────────────────────────────────────

    async def _run_forever(self):
        while True:
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Connection error: %s — retry in %ds", e, RECONNECT_DELAY)
            self._connected = False
            self._last_heartbeat = 0.0
            if self._on_disconnect_cb:
                self._on_disconnect_cb()
            await asyncio.sleep(RECONNECT_DELAY)

    async def _connect(self):
        # 저장된 guild_id가 있으면 항상 우선 (만료된 pair_code 무시)
        saved_guild = self._load_guild_id()
        if saved_guild:
            await self._connect_with_guild(saved_guild)
            return

        if not self._pair_code:
            logger.debug("No pair code — waiting...")
            await asyncio.sleep(5)
            return

        logger.info("Connecting to %s", config.RELAY_SERVER_URL)
        async with websockets.connect(
            config.RELAY_SERVER_URL,
            open_timeout=60,
            ping_interval=30,
            ping_timeout=20,
        ) as ws:
            self._ws = ws
            _set_keepalive(ws)

            await ws.send(json.dumps({
                "type":   "pair",
                "code":   self._pair_code,
                "secret": config.RELAY_SECRET,
            }))

            async for raw in ws:
                msg = json.loads(raw)
                await self._handle(msg)

    async def _connect_with_guild(self, guild_id: str):
        """저장된 guild_id로 secret만으로 재연결"""
        logger.info("Reconnecting with saved guild_id=%s", guild_id)
        async with websockets.connect(
            config.RELAY_SERVER_URL,
            open_timeout=60,
            ping_interval=30,
            ping_timeout=20,
        ) as ws:
            self._ws = ws
            _set_keepalive(ws)

            await ws.send(json.dumps({
                "type":     "reconnect",
                "guild_id": guild_id,
                "secret":   config.RELAY_SECRET,
            }))

            async for raw in ws:
                msg = json.loads(raw)
                await self._handle(msg)

    # ── 메시지 처리 ───────────────────────────────────────────────────────────

    async def _handle(self, msg: dict):
        mtype = msg.get("type")

        if mtype == "pair_ok":
            self._guild_id = msg["guild_id"]
            self._connected = True
            self._pair_code = None   # 코드 소모됨
            self._save_guild_id(self._guild_id)
            self._clear_pair_code_env()
            self._last_heartbeat = time.time()              # heartbeat 기준점 초기화
            self._bot_discord = msg.get("bot_discord", True)  # 연결 즉시 동기화
            logger.info("Paired with guild %s", self._guild_id)
            self._check_server_version(msg.get("server_version", ""))
            if self._on_connect_cb:
                self._on_connect_cb(self._guild_id)
            await self._push_state()

        elif mtype == "heartbeat":
            self._last_heartbeat = time.time()
            self._bot_discord = msg.get("bot_discord", self._bot_discord)
            logger.debug("Heartbeat: bot_discord=%s", self._bot_discord)

        elif mtype == "bot_status":
            self._bot_discord = msg.get("bot_discord", self._bot_discord)
            logger.info("Bot Discord status: %s", "online" if self._bot_discord else "offline")

        elif mtype == "error":
            raw = msg.get("message", "알 수 없는 오류")
            logger.error("Relay error: %s", raw)
            if "만료" in raw or "유효하지" in raw:
                user_msg = (f"⚠️ 코드가 만료됐거나 올바르지 않습니다.\n"
                            f"Discord에서 /setup을 다시 실행해 새 코드를 받으세요.")
            elif "인증" in raw:
                user_msg = "❌ 서버 인증 실패 — 설정을 확인하세요."
            else:
                user_msg = f"❌ 릴레이 오류: {raw}"
            if self._on_error_cb:
                self._on_error_cb(user_msg)

        elif mtype == "command":
            asyncio.create_task(self._exec_command(msg))

    async def _exec_command(self, msg: dict):
        cmd_id = msg.get("cmd_id")
        cmd    = msg.get("cmd")
        args   = msg.get("args", {})

        try:
            result = await self._dispatch(cmd, args)
        except Exception as e:
            logger.error("Command error [%s]: %s", cmd, e)
            result = f"❌ 오류: {e}"

        await self._send({"type": "response", "cmd_id": cmd_id, "content": result})

    async def _dispatch(self, cmd: str, args: dict) -> str:
        loop = asyncio.get_event_loop()

        if cmd == "dl":
            try:
                task = self.dl.enqueue(args["url"], args.get("user", "?"))
                return f"✅ #{task.id} 대기열 추가됨"
            except ValueError as e:
                return f"❌ {e}"

        elif cmd == "cancel":
            tid = args.get("task_id")
            if self.dl.cancel(tid):
                return f"⏹️ #{tid} 취소 중..."
            return f"❌ #{tid} 찾을 수 없음"

        elif cmd == "waiting":
            s = self.dl.status()
            lines = []
            if s["active"]:
                lines.append("**진행 중:**")
                for t in s["active"]:
                    if t.get("state") == "live":
                        lines.append(f" `#{t['id']}` 🔴 {t.get('downloaded') or '?'} | {t.get('speed') or '?'}")
                    else:
                        lines.append(f" `#{t['id']}` {t.get('progress', 0):.1f}% {t.get('speed') or '?'} ETA {t.get('eta') or '?'}")
            if s["queued"]:
                lines.append(f"**대기:** {s['queued']}개")
            if not lines:
                lines.append("📭 대기열 없음")
            return "\n".join(lines)

        elif cmd == "login":
            result_holder = {"msg": ""}
            done = threading.Event()

            def on_done(ok):
                if ok:
                    result_holder["msg"] = "✅ 로그인 성공! 쿠키가 저장되었습니다."
                else:
                    result_holder["msg"] = (
                        "❌ 로그인이 취소됐거나 실패했습니다.\n"
                        "Edge 창을 X로 닫은 경우 `/login`을 다시 입력하세요."
                    )
                done.set()

            def on_progress(msg):
                asyncio.run_coroutine_threadsafe(
                    self._event(msg), self._loop)

            self.cm.login_flow(on_done=on_done, on_progress=on_progress)
            await loop.run_in_executor(None, lambda: done.wait(timeout=280))
            return result_holder["msg"] or "⏱️ 로그인 대기 시간 초과"

        elif cmd == "unarchived_add":
            if not self.sw:
                return "❌ Watcher 비활성화"
            from stream_watcher import StreamWatcher
            label = self.sw.add(args["url"], args.get("name", ""), args.get("filter", "unarchived"))
            filt  = args.get("filter", "unarchived") or "전체 라이브"
            if self._on_watcher_change_cb:
                self._on_watcher_change_cb()
            return f"✅ **{label}** 등록 완료 (필터: `{filt}`)"

        elif cmd == "unarchived_remove":
            if not self.sw:
                return "❌ Watcher 비활성화"
            name = args.get("name", "")
            result = self.sw.remove(name)
            if result and self._on_watcher_change_cb:
                self._on_watcher_change_cb()
            return f"🗑️ **{name}** 해제됨" if result else f"❌ `{name}` 찾을 수 없음"

        elif cmd == "unarchived_list":
            if not self.sw:
                return "❌ Watcher 비활성화"
            from stream_watcher import StreamWatcher
            channels = self.sw.list_channels()
            if not channels:
                return "📭 등록된 채널 없음"
            lines = [f"**Unarchived 감지 목록** (폴링: {config.WATCH_POLL_INTERVAL // 60}분)"]
            for url, info in channels:
                filt  = f"`{info['title_filter']}`" if info["title_filter"] else "`전체 라이브`"
                label = StreamWatcher.display_name(info)
                lines.append(f"• **{label}** — {filt}\n  {url}")
            return "\n".join(lines)

        elif cmd == "unarchived_check":
            if not self.sw:
                return "❌ Watcher 비활성화"
            return await self.sw.check_now()

        return f"❌ 알 수 없는 명령: {cmd}"

    # ── 이벤트 전송 ───────────────────────────────────────────────────────────

    async def _event(self, content: str):
        if self._guild_id:
            await self._send({
                "type":     "event",
                "guild_id": self._guild_id,
                "content":  content,
            })

    async def _push_state(self):
        """다운로드 상태를 서버에 push (autocomplete용)"""
        if not self._guild_id:
            return
        await self._send({
            "type":     "state",
            "guild_id": self._guild_id,
            "data":     self.dl.status(),
        })

    # ── guild_id 영속 저장 ────────────────────────────────────────────────────

    def _clear_pair_code_env(self):
        env_path = os.path.join(config.BASE_DIR, ".env")
        tmp_path = env_path + ".tmp"
        try:
            with open(env_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
            import re
            content = re.sub(r"^RELAY_PAIR_CODE=.*$", "RELAY_PAIR_CODE=", content, flags=re.MULTILINE)
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, env_path)
        except Exception as e:
            logger.debug("clear pair code env: %s", e)
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    def _save_guild_id(self, guild_id: str):
        fpath = config.RELAY_GUILD_FILE
        tmp = fpath + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(guild_id)
            os.replace(tmp, fpath)
        except Exception as e:
            logger.debug("guild_id save error: %s", e)
            try:
                os.remove(tmp)
            except Exception:
                pass

    def _load_guild_id(self) -> str:
        try:
            fpath = config.RELAY_GUILD_FILE
            if os.path.exists(fpath):
                with open(fpath) as f:
                    return f.read().strip()
        except Exception:
            pass
        return ""

    def _check_server_version(self, server_ver: str):
        if not server_ver:
            return
        def _parse(v):
            try:
                return tuple(int(x) for x in v.split(".")[:2])
            except ValueError:
                return (0, 0)
        sv = _parse(server_ver)
        cv = _parse(config.APP_VERSION)
        if sv < cv:
            msg = (f"⚠️ 릴레이 서버 버전({server_ver})이 낮습니다 "
                   f"(클라이언트: {config.APP_VERSION}). 서버 업데이트가 필요합니다.")
            logger.warning(msg)
            if self._on_error_cb:
                self._on_error_cb(msg)

    async def _send(self, payload: dict):
        if self._ws:
            try:
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                logger.debug("send error: %s", e)

    # ── 다운로드 이벤트 → relay ───────────────────────────────────────────────

    def _on_dl_event(self, event, task, **kw):
        if not self._connected:
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_dl_event(event, task, **kw), self._loop)

    async def _handle_dl_event(self, event, task, **kw):
        if event in ("queued", "info_start"):
            await self._push_state()
            return

        if event == "start":
            title = (task.info.get("title", "?") if task.info else "?")[:80]
            mem   = "🔒 " if task.is_membership else ""
            state_label = {"live": "🔴 라이브", "normal": "🎬 VOD"}.get(task.state, "?")
            await self._event(
                f"⬇️ {mem}**{title}**\n`{state_label}` | 작업 #{task.id}")

        elif event == "progress":
            if task.state == "live":
                await self._event(
                    f"📡 #{task.id} 라이브 수신: **{task.downloaded or '?'}** | {task.speed or '?'}")
            else:
                await self._event(
                    f"📥 #{task.id} {task.progress:.1f}% | {task.speed or '?'} | ETA {task.eta or '?'}")

        elif event == "completed":
            title = (task.info.get("title", "?") if task.info else "?")[:60]
            await self._event(f"✅ #{task.id} 완료: **{title}**")

        elif event == "failed":
            await self._event(f"❌ #{task.id} 실패: {task.error}")

        elif event == "cancelled":
            await self._event(f"⏹️ #{task.id} 취소됨")

        elif event == "warning":
            await self._event(f"⚠️ {kw.get('message', '')}")

        await self._push_state()
