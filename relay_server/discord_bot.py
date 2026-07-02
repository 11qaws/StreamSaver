"""
StreamSaver Discord 봇 — server.py와 별도 프로세스
server.py IPC(localhost:8766)에 연결해 명령을 주고받음.

systemd: streamsaver-bot.service (Restart=always)
Discord 재연결이 이 프로세스를 얼어붙혀도 server.py 에는 영향 없음.

IPC 메시지 형식 (newline-delimited JSON):
  Bot → Hub: {"t":"bot_up"} / {"t":"bot_down"} / {"t":"cmd",...} / {"t":"setup",...}
  Hub → Bot: {"t":"send","cid":"...","msg":"..."} / {"t":"resp","id":"...","msg":"..."} /
             {"t":"setup_resp","id":"...","connected":bool,"code":"..."} /
             {"t":"state_resp","id":"...","data":{...}}
"""
import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("DiscordBot")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
IPC_HOST      = os.getenv("IPC_HOST", "127.0.0.1")
IPC_PORT      = int(os.getenv("IPC_PORT", "8766"))
BOT_VERSION   = "1.2.4"

# cmd cooldown 추적
_cooldown: dict[str, float] = {}
COOLDOWN_SEC = 3.0

# IPC 응답 대기 futures
_pending: dict[str, asyncio.Future] = {}

# 다운로드 상태 캐시 (guild_id → data)
_dl_state: dict[str, dict] = {}

# ── IPC 클라이언트 ────────────────────────────────────────────────────────────

_writer: Optional[asyncio.StreamWriter] = None
_bot_ref: Optional[commands.Bot] = None


async def ipc_send(msg: dict):
    w = _writer
    if not w:
        return
    try:
        w.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await w.drain()
    except Exception as e:
        logger.warning("IPC send error: %s", e)


async def ipc_call(msg: dict, timeout: float = 12.0) -> Optional[dict]:
    req_id = str(uuid.uuid4())
    msg["id"] = req_id
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _pending[req_id] = fut
    try:
        await ipc_send(msg)
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        _pending.pop(req_id, None)


async def ipc_reader_loop(reader: asyncio.StreamReader):
    global _bot_ref
    async for raw in reader:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = msg.get("t")
        req_id = msg.get("id")

        if t in ("resp", "setup_resp", "state_resp"):
            fut = _pending.get(req_id)
            if fut and not fut.done():
                fut.set_result(msg)

        elif t == "send":
            cid = msg.get("cid")
            text = msg.get("msg", "")
            if cid and _bot_ref and text:
                ch = _bot_ref.get_channel(int(cid))
                if ch:
                    try:
                        await ch.send(text)
                    except Exception as e:
                        logger.warning("channel.send error ch=%s: %s", cid, e)

        elif t == "state_push":
            gid = msg.get("gid")
            if gid:
                _dl_state[gid] = msg.get("data", {})


async def connect_ipc():
    global _writer
    delay = 5
    while True:
        try:
            reader, writer = await asyncio.open_connection(IPC_HOST, IPC_PORT)
            _writer = writer
            logger.info("IPC connected to hub %s:%d", IPC_HOST, IPC_PORT)
            await ipc_send({"t": "bot_up"})
            delay = 5
            await ipc_reader_loop(reader)
        except (ConnectionRefusedError, OSError) as e:
            logger.warning("IPC connect failed: %s — retry in %ds", e, delay)
        except Exception as e:
            logger.error("IPC error: %s — retry in %ds", e, delay)
        finally:
            _writer = None
            try:
                if "writer" in dir():
                    writer.close()
            except Exception:
                pass

        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


# ── Discord 봇 ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True


class StreamBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.add_cog(RelayCog(self))
        await self.tree.sync()
        logger.info("Commands synced")


def _cooldown_ok(gid: str) -> bool:
    now = time.monotonic()
    last = _cooldown.get(gid, 0)
    if now - last < COOLDOWN_SEC:
        return False
    _cooldown[gid] = now
    return True


def _validate_url(url: str) -> bool:
    if len(url) > 2048:
        return False
    return bool(re.match(r'^https?://', url))


class RelayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        global _bot_ref
        _bot_ref = self.bot
        logger.info("Discord ready: %s (guilds=%d)", self.bot.user, len(self.bot.guilds))
        # IPC에 연결 알림은 connect_ipc()에서 이미 수행

    @commands.Cog.listener()
    async def on_disconnect(self):
        await ipc_send({"t": "bot_down"})

    @commands.Cog.listener()
    async def on_resumed(self):
        await ipc_send({"t": "bot_up"})

    # ── Slash: /setup ────────────────────────────────────────────────────────
    @app_commands.command(name="setup", description="이 채널에 StreamSaver를 연결합니다")
    @app_commands.default_permissions(administrator=True)
    async def slash_setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gid = str(interaction.guild_id)
        cid = interaction.channel_id

        resp = await ipc_call({"t": "setup", "gid": gid, "cid": cid})
        if resp is None:
            await interaction.followup.send("❌ 허브 서버 응답 없음", ephemeral=True)
            return

        if resp.get("connected"):
            await interaction.followup.send("✅ 이미 PC가 연결되어 있습니다!", ephemeral=True)
        else:
            code = resp.get("code", "???")
            await interaction.followup.send(
                f"🔗 StreamSaver PC의 대시보드 → **서버 연결** 에서 코드를 입력하세요:\n"
                f"## `{code}`\n"
                f"코드는 10분간 유효합니다.",
                ephemeral=True,
            )

    # ── Slash: /download ─────────────────────────────────────────────────────
    @app_commands.command(name="download", description="YouTube 영상을 다운로드합니다")
    @app_commands.describe(url="YouTube URL", quality="화질 선택 (기본: best)")
    @app_commands.choices(quality=[
        app_commands.Choice(name="최고화질 (best)", value="best"),
        app_commands.Choice(name="1080p",          value="1080"),
        app_commands.Choice(name="720p",           value="720"),
        app_commands.Choice(name="480p",           value="480"),
        app_commands.Choice(name="오디오만 (mp3)", value="audio"),
    ])
    async def slash_dl(self, interaction: discord.Interaction, url: str,
                       quality: str = "best"):
        gid = str(interaction.guild_id)
        if not _cooldown_ok(gid):
            await interaction.response.send_message(
                f"⏳ 너무 빠릅니다. {COOLDOWN_SEC:.0f}초 후 다시 시도하세요.", ephemeral=True)
            return
        if not _validate_url(url):
            await interaction.response.send_message("❌ 유효하지 않은 URL입니다.", ephemeral=True)
            return

        await interaction.response.defer()
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "dl",
             "args": {"url": url, "quality": quality}},
            timeout=15,
        )
        if resp is None:
            await interaction.followup.send(
                "⏱️ 응답 없음 — PC가 켜져 있고 StreamSaver가 실행 중인지 확인하세요.")
        else:
            await interaction.followup.send(resp.get("msg", "✅"))

    # ── Slash: /status ───────────────────────────────────────────────────────
    @app_commands.command(name="status", description="현재 다운로드 상태를 확인합니다")
    async def slash_status(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        if not _cooldown_ok(gid):
            await interaction.response.send_message(
                f"⏳ {COOLDOWN_SEC:.0f}초 후 다시 시도하세요.", ephemeral=True)
            return

        await interaction.response.defer()
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "status", "args": {}},
            timeout=12,
        )
        if resp is None:
            await interaction.followup.send(
                "⏱️ 응답 없음 — StreamSaver PC가 연결되어 있는지 확인하세요.")
        else:
            await interaction.followup.send(resp.get("msg", "✅"))

    # ── Slash: /cancel ───────────────────────────────────────────────────────
    @app_commands.command(name="cancel", description="진행 중인 다운로드를 취소합니다")
    @app_commands.describe(task_id="취소할 작업 ID (없으면 전체 취소)")
    async def slash_cancel(self, interaction: discord.Interaction,
                           task_id: Optional[str] = None):
        gid = str(interaction.guild_id)
        if not _cooldown_ok(gid):
            await interaction.response.send_message(
                f"⏳ {COOLDOWN_SEC:.0f}초 후 다시 시도하세요.", ephemeral=True)
            return

        await interaction.response.defer()
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "cancel",
             "args": {"task_id": task_id}},
            timeout=12,
        )
        if resp is None:
            await interaction.followup.send("⏱️ 응답 없음")
        else:
            await interaction.followup.send(resp.get("msg", "✅"))

    # ── Slash: /login ────────────────────────────────────────────────────────
    @app_commands.command(name="login", description="Edge 브라우저로 YouTube 로그인을 시작합니다")
    async def slash_login(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        if not _cooldown_ok(gid):
            await interaction.response.send_message(
                f"⏳ {COOLDOWN_SEC:.0f}초 후 다시 시도하세요.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "login", "args": {}},
            timeout=12,
        )
        if resp is None:
            await interaction.followup.send("⏱️ 응답 없음", ephemeral=True)
        else:
            await interaction.followup.send(resp.get("msg", "✅"), ephemeral=True)

    # ── Slash: /restart ──────────────────────────────────────────────────────
    @app_commands.command(name="restart", description="StreamSaver를 재시작합니다")
    @app_commands.default_permissions(administrator=True)
    async def slash_restart(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "restart", "args": {}},
            timeout=12,
        )
        if resp is None:
            await interaction.followup.send("⏱️ 응답 없음", ephemeral=True)
        else:
            await interaction.followup.send(resp.get("msg", "✅"), ephemeral=True)

    # ── Slash: /help ─────────────────────────────────────────────────────────
    @app_commands.command(name="help", description="StreamSaver 사용법을 안내합니다")
    async def slash_help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="StreamSaver 도움말",
            description=f"버전 {BOT_VERSION}",
            color=0x3B82F6,
        )
        embed.add_field(name="/setup",   value="이 채널에 PC 연결", inline=False)
        embed.add_field(name="/download [url]", value="YouTube 다운로드 시작", inline=False)
        embed.add_field(name="/status",  value="다운로드 현황 확인", inline=False)
        embed.add_field(name="/cancel",  value="다운로드 취소", inline=False)
        embed.add_field(name="/login",   value="YouTube 로그인 갱신", inline=False)
        embed.add_field(name="/restart", value="앱 재시작 (관리자)", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── 레거시 !명령어 (호환용) ──────────────────────────────────────────────
    @commands.command(name="dl")
    async def cmd_dl(self, ctx: commands.Context, *, url: str = ""):
        if not url or not _validate_url(url):
            await ctx.reply("❌ 올바른 URL을 입력하세요.")
            return
        gid = str(ctx.guild.id)
        if not _cooldown_ok(gid):
            await ctx.reply(f"⏳ {COOLDOWN_SEC:.0f}초 후 다시 시도하세요.")
            return
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "dl",
             "args": {"url": url, "quality": "best"}},
            timeout=15,
        )
        await ctx.reply(resp.get("msg", "✅") if resp else "⏱️ 응답 없음")

    @commands.command(name="상태")
    async def cmd_status(self, ctx: commands.Context):
        gid = str(ctx.guild.id)
        if not _cooldown_ok(gid):
            await ctx.reply(f"⏳ {COOLDOWN_SEC:.0f}초 후 다시 시도하세요.")
            return
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "status", "args": {}}, timeout=12)
        await ctx.reply(resp.get("msg", "✅") if resp else "⏱️ 응답 없음")

    @commands.command(name="취소")
    async def cmd_cancel(self, ctx: commands.Context, task_id: str = ""):
        gid = str(ctx.guild.id)
        if not _cooldown_ok(gid):
            await ctx.reply(f"⏳ {COOLDOWN_SEC:.0f}초 후 다시 시도하세요.")
            return
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "cancel",
             "args": {"task_id": task_id or None}},
            timeout=12,
        )
        await ctx.reply(resp.get("msg", "✅") if resp else "⏱️ 응답 없음")

    @commands.command(name="로그인")
    async def cmd_login(self, ctx: commands.Context):
        gid = str(ctx.guild.id)
        if not _cooldown_ok(gid):
            await ctx.reply(f"⏳ {COOLDOWN_SEC:.0f}초 후 다시 시도하세요.")
            return
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "login", "args": {}}, timeout=12)
        await ctx.reply(resp.get("msg", "✅") if resp else "⏱️ 응답 없음")

    @commands.command(name="재시작")
    @commands.has_permissions(administrator=True)
    async def cmd_restart(self, ctx: commands.Context):
        gid = str(ctx.guild.id)
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "restart", "args": {}}, timeout=12)
        await ctx.reply(resp.get("msg", "✅") if resp else "⏱️ 응답 없음")


# ── 진입점 ───────────────────────────────────────────────────────────────────

async def main():
    bot = StreamBot()

    # IPC 연결 (백그라운드)
    asyncio.create_task(connect_ipc())

    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
