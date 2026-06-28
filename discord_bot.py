import asyncio
import logging
import os
import discord
from discord import app_commands
from discord.ext import commands

import config

logger = logging.getLogger("StreamSaver.Bot")


class _RateLimiter:
    """Discord 429 억제 — 연속 메시지 간격 강제"""
    def __init__(self, min_gap: float = 0.8):
        self._min_gap = min_gap
        self._last    = 0.0
        self._lock    = asyncio.Lock()

    async def send(self, channel, content):
        async with self._lock:
            now  = asyncio.get_event_loop().time()
            wait = self._min_gap - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            await channel.send(content)
            self._last = asyncio.get_event_loop().time()


class StreamSaverCog(commands.Cog):
    def __init__(self, bot):
        self.bot      = bot
        self.dl       = bot.dl
        self.cm       = bot.cm
        self._channel = None
        self._ready   = False
        self._rl      = _RateLimiter(min_gap=0.8)
        self._threads: dict = {}   # task_id → discord.Thread

        self.dl.on_event(self._on_dl_event)

    # ── 리스너 ────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Bot logged in as %s", self.bot.user)
        self._channel = self.bot.get_channel(config.DISCORD_CHANNEL_ID)
        if self._channel and not self._ready:
            await self._channel.send("✅ StreamSaver 온라인")
            self._ready = True
        if self.bot.gui:
            self.bot.gui.set_bot_connected(True)
            self.bot.gui.notify("StreamSaver 온라인", f"{self.bot.user} 연결됨")
        # 슬래시 커맨드 즉시 반영 (guild sync)
        if self._channel:
            guild = self._channel.guild
            self.bot.tree.copy_global_to(guild=guild)
            await self.bot.tree.sync(guild=guild)
            logger.info("Slash commands synced to guild %s", guild.id)

    @commands.Cog.listener()
    async def on_disconnect(self):
        if self.bot.gui:
            self.bot.gui.set_bot_connected(False)

    # ── 다운로드 이벤트 → Discord ─────────────────────────────────────────────

    def _on_dl_event(self, event, task, **kw):
        if not self._ready or not self._channel:
            return
        asyncio.run_coroutine_threadsafe(
            self._dispatch_dl_event(event, task, **kw), self.bot.loop)

    async def _dispatch_dl_event(self, event, task, **kw):
        ch = self._channel

        if event in ("queued", "info_start"):
            return

        elif event == "start":
            title = task.info.get("title", "?") if task.info else "?"
            short = title[:80] + ("…" if len(title) > 80 else "")
            mem   = "🔒 " if task.is_membership else ""
            state_label = {"live": "🔴 라이브", "normal": "🎬 VOD"}.get(task.state, task.state or "?")
            msg = await ch.send(
                f"⬇️ {mem}**{short}**\n"
                f"`{state_label}` | 작업 #{task.id}")
            try:
                thread = await msg.create_thread(
                    name=f"작업{task.id} 진행상황",
                    auto_archive_duration=60)
                self._threads[task.id] = thread
                await thread.send(
                    f"📋 요청자: {task.requested_by}\n"
                    f"🔗 {task.url}")
            except Exception as e:
                logger.warning("Thread 생성 실패: %s", e)

        elif event == "progress":
            thread = self._threads.get(task.id)
            target = thread or ch
            if task.state == "live":
                dl  = task.downloaded or "?"
                spd = task.speed or "?"
                line = f"📡 #{task.id} 라이브 수신: **{dl}** | {spd}"
            else:
                line = (f"📥 #{task.id} {task.progress:.1f}%"
                        f" | {task.speed or '?'} | ETA {task.eta or '?'}")
            await self._rl.send(target, line)

        elif event == "completed":
            thread = self._threads.pop(task.id, None)
            title  = task.info.get("title", "?") if task.info else "?"
            await self._rl.send(ch, f"✅ #{task.id} 완료: **{title[:60]}**")
            if thread:
                try:
                    await thread.send("✅ 다운로드 완료!")
                except Exception:
                    pass
            if self.bot.gui:
                self.bot.gui.notify("✅ 다운로드 완료", title[:80])

        elif event == "failed":
            self._threads.pop(task.id, None)
            await self._rl.send(ch, f"❌ #{task.id} 실패: {task.error}")
            if self.bot.gui:
                self.bot.gui.notify("❌ 다운로드 실패", task.error or "오류 발생")

        elif event == "cancelled":
            self._threads.pop(task.id, None)
            await self._rl.send(ch, f"⏹️ #{task.id} 취소됨")

        elif event == "warning":
            await self._rl.send(ch, f"⚠️ {kw.get('message', '')}")

    # ── 유틸 ──────────────────────────────────────────────────────────────────

    async def _wrong_channel(self, interaction: discord.Interaction) -> bool:
        if interaction.channel_id != config.DISCORD_CHANNEL_ID:
            await interaction.response.send_message(
                "❌ 지정 채널에서만 사용 가능합니다.", ephemeral=True)
            return True
        return False

    # ── 슬래시 커맨드 ─────────────────────────────────────────────────────────

    @app_commands.command(name="dl", description="YouTube 영상 다운로드")
    @app_commands.describe(url="YouTube URL")
    async def cmd_dl(self, interaction: discord.Interaction, url: str):
        if await self._wrong_channel(interaction):
            return
        task = self.dl.enqueue(url, interaction.user.name)
        await interaction.response.send_message(f"✅ #{task.id} 대기열 추가됨")

    # ── /cancel ──────────────────────────────────────────────────────────────

    @app_commands.command(name="cancel", description="다운로드 작업 취소")
    @app_commands.describe(task_id="취소할 작업 (목록에서 선택)")
    async def cmd_cancel(self, interaction: discord.Interaction, task_id: int):
        if self.dl.cancel(task_id):
            await interaction.response.send_message(f"⏹️ #{task_id} 취소 중...")
        else:
            await interaction.response.send_message(
                f"❌ #{task_id} 찾을 수 없음 — `/waiting` 으로 현재 목록을 확인하세요.")

    @cmd_cancel.autocomplete("task_id")
    async def _cancel_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        s = self.dl.status()
        choices: list[app_commands.Choice[int]] = []

        for t in s.get("active", []):
            tid = t["id"]
            if t.get("state") == "live":
                label = f"#{tid} 🔴 라이브 · {t.get('downloaded') or '?'} · {t.get('speed') or '?'}"
            else:
                prog = t.get("progress") or 0
                label = f"#{tid} ⬇️ {prog:.0f}% · {t.get('speed') or '?'} · ETA {t.get('eta') or '?'}"
            choices.append(app_commands.Choice(name=label[:100], value=tid))

        for t in s.get("queue_list", []):
            tid = t["id"]
            url_short = t.get("url", "")[-40:]
            label = f"#{tid} ⏳ 대기 중 · {url_short}"
            choices.append(app_commands.Choice(name=label[:100], value=tid))

        if current:
            choices = [c for c in choices if current in str(c.value) or current in c.name]

        return choices[:25]

    # ── 나머지 커맨드 ─────────────────────────────────────────────────────────

    @app_commands.command(name="waiting", description="진행 중 / 대기 목록")
    async def cmd_queue(self, interaction: discord.Interaction):
        s = self.dl.status()
        lines = []
        if s["active"]:
            lines.append("**진행 중:**")
            for t in s["active"]:
                if t.get("state") == "live":
                    lines.append(f" `#{t['id']}` 🔴 {t['downloaded'] or '?'} | {t['speed']}")
                else:
                    lines.append(f" `#{t['id']}` {t['progress']:.1f}% {t['speed']} ETA {t['eta']}")
        if s["queued"]:
            lines.append(f"**대기:** {s['queued']}개")
        if not lines:
            lines.append("📭 대기열 없음")
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="status", description="봇 전체 상태 확인")
    async def cmd_status(self, interaction: discord.Interaction):
        cs = self.cm.get_status()
        ds = self.dl.status()
        await interaction.response.send_message(
            f"**StreamSaver 상태**\n"
            f"🔑 쿠키: {'✅ 유효' if cs['cookie_valid'] else '❌ 없음'}\n"
            f"📄 쿠키파일: {'✅' if cs['cookie_file'] else '❌'} ({cs['cookie_size']}B)\n"
            f"🖥️ Edge: {cs['edge_state']}\n"
            f"⬇️ 진행 중: {len(ds['active'])}개\n"
            f"📋 대기: {ds['queued']}개")

    @app_commands.command(name="login", description="YouTube 로그인 (멤버십 다운로드용)")
    async def cmd_login(self, interaction: discord.Interaction):
        if self.cm._login_lock.locked():
            await interaction.response.send_message(
                "⏳ 이미 로그인 진행 중입니다.", ephemeral=True)
            return

        await interaction.response.send_message(
            "🌐 로그인 플로우를 시작합니다.\n"
            "Edge 창이 열리면 YouTube에 로그인해 주세요.\n"
            "로그인이 감지되면 자동으로 쿠키를 저장합니다.")

        loop = self.bot.loop
        channel = interaction.channel

        def progress_callback(msg):
            asyncio.run_coroutine_threadsafe(
                self._rl.send(channel, msg), loop)

        self.cm.login_flow(
            on_done=lambda ok: asyncio.run_coroutine_threadsafe(
                self._login_result(channel, ok), loop),
            on_progress=progress_callback,
        )

    async def _login_result(self, channel, ok):
        if ok:
            await channel.send("✅ 로그인 성공! 쿠키가 저장되었습니다.")
        else:
            await channel.send("❌ 로그인 실패. `/debug` 로 상세 진단을 확인하세요.")

    @app_commands.command(name="cookie", description="CDP 및 쿠키 상태 확인")
    async def cmd_cookie_test(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        result = await self.bot.loop.run_in_executor(None, self.cm.test_cdp)
        await interaction.followup.send(result)

    @app_commands.command(name="debug", description="Edge 프로세스 진단")
    async def cmd_edge_debug(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        result = await self.bot.loop.run_in_executor(None, self.cm.debug_edge)
        if len(result) > 1900:
            result = result[:1900] + "\n...(잘림)"
        await interaction.followup.send(result)

    @app_commands.command(name="restart", description="headless Edge CDP 재시작")
    async def cmd_edge_restart(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        def _restart():
            self.cm._restart_headless()
            return self.cm._is_running()

        ok = await self.bot.loop.run_in_executor(None, _restart)
        if ok:
            await interaction.followup.send("✅ Edge CDP 재연결 성공")
        else:
            await interaction.followup.send(
                f"❌ Edge CDP 재연결 실패 (포트 {self.cm.cdp_port})\n"
                f"`/debug` 로 원인을 확인하세요.")

    @app_commands.command(name="trace", description="최근 로그 확인")
    async def cmd_log(self, interaction: discord.Interaction):
        log_dir = config.LOG_DIR
        if not os.path.isdir(log_dir):
            await interaction.response.send_message("📭 로그 없음")
            return
        logs = sorted(os.listdir(log_dir), reverse=True)
        if not logs:
            await interaction.response.send_message("📭 로그 없음")
            return
        target = os.path.join(log_dir, logs[0])
        try:
            with open(target, "r", encoding="utf-8") as f:
                lines = f.readlines()
            tail = "".join(lines[-15:])
            text = f"**최근 로그 ({logs[0]})**\n```\n{tail}\n```"
            if len(text) > 1900:
                tail = "".join(lines[-8:])
                text = f"**최근 로그 ({logs[0]})**\n```\n{tail}\n```"
            await interaction.response.send_message(text)
        except Exception as e:
            await interaction.response.send_message(f"❌ 로그 읽기 실패: {e}")

    @app_commands.command(name="help", description="StreamSaver 명령어 안내")
    async def cmd_help(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "**StreamSaver 명령어**\n"
            "`/dl url` — YouTube 다운로드\n"
            "`/cancel task_id` — 작업 취소 (목록 자동완성 지원)\n"
            "`/waiting` — 진행 중 / 대기 목록\n"
            "`/status` — 봇 전체 상태\n"
            "`/login` — YouTube 로그인 (멤버십용)\n"
            "`/cookie` — CDP 쿠키 상태 확인\n"
            "`/debug` — Edge 프로세스 진단\n"
            "`/restart` — headless Edge 재시작\n"
            "`/trace` — 최근 로그 확인",
            ephemeral=True)


class StreamSaverBot(commands.Bot):
    def __init__(self, downloader, cookie_manager, gui=None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.dl  = downloader
        self.cm  = cookie_manager
        self.gui = gui

    async def setup_hook(self):
        await self.add_cog(StreamSaverCog(self))
