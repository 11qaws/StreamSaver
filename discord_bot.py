import asyncio
import logging
import os
import discord
from discord.ext import commands

import config

logger = logging.getLogger("StreamSaver.Bot")


class StreamSaverCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dl = bot.dl
        self.cm = bot.cm
        self._channel = None
        self._ready = False

        self.dl.on_event(self._on_dl_event)

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"Bot logged in as {self.bot.user}")
        self._channel = self.bot.get_channel(config.DISCORD_CHANNEL_ID)
        if self._channel:
            await self._channel.send("StreamSaver 온라인")
            self._ready = True

    def _on_dl_event(self, event, task, **kw):
        if not self._ready or not self._channel:
            return
        coro = self._dispatch_dl_event(event, task, **kw)
        asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

    async def _dispatch_dl_event(self, event, task, **kw):
        if event == "queued":
            await self._channel.send(
                f"✅ 대기열 추가: `{task.url[:60]}...` (#{task.id})")
        elif event == "info_start":
            await self._channel.send(f"🔍 정보 확인 중: `{task.url[:60]}...`")
        elif event == "start":
            title = task.info.get("title", "?") if task.info else "?"
            state = task.state or "?"
            mem = "🔒 [멤버십] " if task.is_membership else ""
            await self._channel.send(
                f"⬇️ {mem}다운로드 시작: **{title}**\n"
                f"상태: {state} | #작업{task.id}")
        elif event == "progress":
            p = task.progress
            s = task.speed or "?"
            e = task.eta or "?"
            await self._channel.send(
                f"📥 #{task.id} 진행: {p:.1f}% | 속도: {s} | ETA: {e}")
        elif event == "completed":
            title = task.info.get("title", "?") if task.info else "?"
            await self._channel.send(
                f"✅ 완료: **{title}** (#{task.id})")
        elif event == "failed":
            await self._channel.send(
                f"❌ 실패 (#{task.id}): {task.error}")
        elif event == "cancelled":
            await self._channel.send(
                f"⏹️ 취소됨 (#{task.id})")
        elif event == "warning":
            msg = kw.get("message", "")
            await self._channel.send(f"⚠️ {msg}")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        await self.bot.process_commands(message)

    @commands.command(name="dl")
    async def cmd_dl(self, ctx, url: str = None):
        if not url:
            await ctx.send("사용법: `!dl <YouTube URL>`")
            return
        if ctx.channel.id != config.DISCORD_CHANNEL_ID:
            return
        task = self.dl.enqueue(url, ctx.author.name)
        await ctx.send(f"✅ 대기열 추가됨 (#{task.id})")

    @commands.command(name="취소")
    async def cmd_cancel(self, ctx, task_id: int = None):
        if not task_id:
            await ctx.send("사용법: `!취소 <작업번호>`")
            return
        if self.dl.cancel(task_id):
            await ctx.send(f"⏹️ #{task_id} 취소 중...")
        else:
            await ctx.send(f"❌ #{task_id} 찾을 수 없음")

    @commands.command(name="대기열")
    async def cmd_queue(self, ctx):
        s = self.dl.status()
        lines = []
        if s["active"]:
            lines.append("**진행 중:**")
            for t in s["active"]:
                lines.append(
                    f" `#{t['id']}` {t['progress']:.1f}% "
                    f"{t['speed']} ETA {t['eta']}")
        if s["queued"]:
            lines.append(f"**대기:** {s['queued']}개")
        if not lines:
            lines.append("📭 대기열 없음")
        await ctx.send("\n".join(lines))

    @commands.command(name="상태")
    async def cmd_status(self, ctx):
        cs = self.cm.get_status()
        ds = self.dl.status()
        await ctx.send(
            f"**StreamSaver 상태**\n"
            f"🔑 쿠키: {'✅ 유효' if cs['cookie_valid'] else '❌ 만료/없음'}\n"
            f"📄 쿠키파일: {'✅ 있음' if cs['cookie_file'] else '❌ 없음'}\n"
            f"⬇️ 진행 중: {len(ds['active'])}개\n"
            f"📋 대기: {ds['queued']}개")

    @commands.command(name="로그인")
    async def cmd_login(self, ctx):
        await ctx.send(
            "🌐 Edge가 실행됩니다. YouTube에 로그인해 주세요.\n"
            "로그인이 감지되면 자동으로 쿠키를 저장하고 "
            "headless 모드로 전환됩니다.")
        self.cm.login_flow(on_done=lambda ok: asyncio.run_coroutine_threadsafe(
            self._login_result(ctx, ok), self.bot.loop))

    async def _login_result(self, ctx, ok):
        if ok:
            await ctx.send("✅ 로그인 성공! 쿠키가 갱신되었습니다.")
        else:
            await ctx.send("❌ 로그인 실패. 다시 시도해 주세요.")

    @commands.command(name="로그")
    async def cmd_log(self, ctx):
        log_dir = config.LOG_DIR
        if not os.path.isdir(log_dir):
            await ctx.send("📭 로그 파일 없음")
            return
        logs = sorted(os.listdir(log_dir), reverse=True)
        if not logs:
            await ctx.send("📭 로그 파일 없음")
            return
        target = os.path.join(log_dir, logs[0])
        try:
            with open(target, "r", encoding="utf-8") as f:
                lines = f.readlines()
            tail = lines[-20:]
            if not tail:
                await ctx.send("📭 빈 로그")
                return
            text = "```\n" + "".join(tail) + "```"
            if len(text) > 1900:
                text = "```\n" + "".join(tail[-10:]) + "```"
            await ctx.send(f"**최근 로그 ({logs[0]})**\n{text}")
        except Exception as e:
            await ctx.send(f"❌ 로그 읽기 오류: {e}")


class StreamSaverBot(commands.Bot):
    def __init__(self, downloader, cookie_manager):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.dl = downloader
        self.cm = cookie_manager

    async def setup_hook(self):
        await self.add_cog(StreamSaverCog(self))
