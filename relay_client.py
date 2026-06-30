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
        self._task       = None
        self._loop       = None
        self._on_connect_cb      = None
        self._on_disconnect_cb   = None
        self._on_watcher_change_cb = None

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
            logger.info("Paired with guild %s", self._guild_id)
            if self._on_connect_cb:
                self._on_connect_cb(self._guild_id)
            await self._push_state()

        elif mtype == "error":
            logger.error("Relay error: %s", msg.get("message"))

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
            task = self.dl.enqueue(args["url"], args.get("user", "?"))
            return f"✅ #{task.id} 대기열 추가됨"

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
                result_holder["msg"] = "✅ 로그인 성공! 쿠키가 저장되었습니다." if ok else "❌ 로그인 실패."
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
        try:
            with open(env_path, "r", encoding="utf-8-sig") as f:
                content = f.read()
            import re
            content = re.sub(r"^RELAY_PAIR_CODE=.*$", "RELAY_PAIR_CODE=", content, flags=re.MULTILINE)
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            logger.debug("clear pair code env: %s", e)

    def _guild_file(self) -> str:
        return os.path.join(config.BASE_DIR, ".relay_guild")

    def _save_guild_id(self, guild_id: str):
        try:
            with open(self._guild_file(), "w") as f:
                f.write(guild_id)
        except Exception as e:
            logger.debug("guild_id save error: %s", e)

    def _load_guild_id(self) -> str:
        try:
            fpath = self._guild_file()
            if os.path.exists(fpath):
                with open(fpath) as f:
                    return f.read().strip()
        except Exception:
            pass
        return ""

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
