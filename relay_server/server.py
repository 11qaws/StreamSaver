"""
StreamSaver Relay Server
Oracle Cloud VPS에서 실행 — Discord 봇 + WebSocket 라우터
다운로드는 하지 않음, 명령 전달만 담당

다중 사용자 격리 원칙:
- 모든 라우팅은 guild_id 기준으로 완전 격리
- 공유 루프(heartbeat, bot_status push)에서 개별 오류가 전체에 영향 없도록 try/except 격리
- 공유 dict 순회 시 list() 스냅샷 사용
- 에러 로그에 항상 guild_id 포함
"""
import asyncio
import json
import logging
import os
import random
import string
import uuid
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Relay")

DISCORD_TOKEN  = os.environ["DISCORD_TOKEN"]
WS_PORT        = int(os.getenv("WS_PORT", "8765"))
WS_SECRET      = os.getenv("WS_SECRET", "")
SERVER_VERSION = "1.0.11"

# ── 상태 ─────────────────────────────────────────────────────────────────────

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# guild_id(str) → {"channel_id": int, "ws": websockets.WebSocketServerProtocol}
guilds: dict[str, dict] = {}

# pair_code → {"guild_id": str, "expires": datetime}
pair_codes: dict[str, dict] = {}

# cmd_id → asyncio.Future  (명령 응답 대기)
pending: dict[str, asyncio.Future] = {}

# guild_id → 다운로드 상태 캐시 (autocomplete용)
dl_state: dict[str, dict] = {}

bot: Optional[commands.Bot] = None
bot_discord_connected: bool = False  # Discord API 연결 상태


# ── 상태 영속화 ───────────────────────────────────────────────────────────────

def _load_state():
    """서버 재시작 후 guild → channel_id 매핑 복구."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for gid, info in data.get("guilds", {}).items():
            ch = info.get("channel_id")
            if gid and ch:
                guilds[gid] = {"channel_id": ch}
        logger.info("State loaded: %d guilds", len(guilds))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("State load error: %s", e)


def _save_state():
    """guild → channel_id 매핑을 파일에 저장."""
    data = {
        "guilds": {
            gid: {"channel_id": info.get("channel_id")}
            for gid, info in guilds.items()
            if info.get("channel_id")
        }
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("State save error: %s", e)


# ── 유틸 ─────────────────────────────────────────────────────────────────────

def _gen_code() -> str:
    letters = string.ascii_uppercase
    digits  = string.digits
    return (
        "".join(random.choices(letters, k=3))
        + "-"
        + "".join(random.choices(digits, k=3))
    )


def _is_connected(guild_id: str) -> bool:
    return guild_id in guilds and "ws" in guilds[guild_id]


async def _push_to_all(payload: str):
    """모든 연결된 PC에 메시지 전송.
    개별 사용자 오류가 다른 사용자에게 영향을 주지 않도록 guild별 예외 격리."""
    for gid, info in list(guilds.items()):  # 스냅샷으로 순회 — dict 변경 안전
        ws = info.get("ws")
        if ws:
            try:
                await ws.send(payload)
            except Exception as e:
                logger.debug("push_to_all guild=%s error: %s", gid, e)


async def _heartbeat_loop():
    """45초마다 모든 PC에 heartbeat 전송.
    릴레이 hang 감지 + 봇 Discord 상태 정기 동기화."""
    while True:
        await asyncio.sleep(45)
        connected_count = sum(1 for g in guilds.values() if g.get("ws"))
        if connected_count == 0:
            continue
        payload = json.dumps({
            "type":        "heartbeat",
            "bot_discord": bot_discord_connected,
        })
        await _push_to_all(payload)
        logger.debug("Heartbeat sent to %d clients", connected_count)


async def _send_cmd(guild_id: str, cmd: str, args: dict, timeout: float = 12.0) -> str:
    """클라이언트에 명령 전달 후 응답 반환. 연결 없으면 에러 메시지 반환."""
    if not _is_connected(guild_id):
        return "❌ StreamSaver PC가 연결되어 있지 않습니다. 프로그램이 실행 중인지 확인하세요."

    cmd_id = str(uuid.uuid4())
    loop   = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    pending[cmd_id] = fut

    payload = json.dumps({
        "type":     "command",
        "cmd_id":   cmd_id,
        "cmd":      cmd,
        "args":     args,
        "guild_id": guild_id,
    })
    try:
        await guilds[guild_id]["ws"].send(payload)
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return "⏱️ 응답 없음 — PC가 켜져 있고 StreamSaver가 실행 중인지 확인하세요."
    except Exception as e:
        logger.error("send_cmd error guild=%s: %s", guild_id, e)
        return f"❌ 오류: {e}"
    finally:
        pending.pop(cmd_id, None)


async def _channel_send(guild_id: str, content: str):
    """이벤트를 Discord 채널로 전송."""
    if guild_id not in guilds:
        return
    ch_id = guilds[guild_id].get("channel_id")
    if not ch_id or not bot:
        return
    ch = bot.get_channel(int(ch_id))
    if ch:
        try:
            await ch.send(content)
        except Exception as e:
            logger.warning("channel_send guild=%s error: %s", guild_id, e)


# ── WebSocket 서버 ────────────────────────────────────────────────────────────

async def ws_handler(ws):
    """각 PC 연결마다 독립 코루틴으로 실행 — 연결 오류가 다른 사용자에게 영향 없음."""
    guild_id: Optional[str] = None
    addr = ws.remote_address
    logger.info("WS connected: %s", addr)

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")

            # ── 재연결 (guild_id + secret) ────────────────────────────────────
            if mtype == "reconnect":
                secret = msg.get("secret", "")
                if WS_SECRET and secret != WS_SECRET:
                    await ws.send(json.dumps({"type": "error", "message": "인증 실패"}))
                    continue
                gid = msg.get("guild_id", "")
                if not gid:
                    await ws.send(json.dumps({"type": "error", "message": "guild_id 없음"}))
                    continue
                if gid not in guilds:
                    guilds[gid] = {}
                guilds[gid]["ws"] = ws
                guild_id = gid
                await ws.send(json.dumps({
                    "type":           "pair_ok",
                    "guild_id":       guild_id,
                    "server_version": SERVER_VERSION,
                    "bot_discord":    bot_discord_connected,
                }))
                logger.info("Reconnected: guild=%s addr=%s", guild_id, addr)
                asyncio.create_task(_channel_send(guild_id, "✅ StreamSaver PC가 재연결되었습니다."))

            # ── 페어링 ───────────────────────────────────────────────────────
            elif mtype == "pair":
                secret = msg.get("secret", "")
                if WS_SECRET and secret != WS_SECRET:
                    await ws.send(json.dumps({"type": "error", "message": "인증 실패"}))
                    continue

                code = msg.get("code", "").strip().upper()
                entry = pair_codes.get(code)
                if not entry or datetime.utcnow() > entry["expires"]:
                    await ws.send(json.dumps({"type": "error", "message": "유효하지 않거나 만료된 코드입니다"}))
                    continue

                guild_id = entry["guild_id"]
                pair_codes.pop(code, None)

                if guild_id not in guilds:
                    guilds[guild_id] = {}
                guilds[guild_id]["ws"] = ws

                await ws.send(json.dumps({
                    "type":           "pair_ok",
                    "guild_id":       guild_id,
                    "server_version": SERVER_VERSION,
                    "bot_discord":    bot_discord_connected,
                }))
                logger.info("Paired: guild=%s addr=%s", guild_id, addr)
                asyncio.create_task(_channel_send(
                    guild_id, "✅ StreamSaver PC가 연결되었습니다."))

            # ── 명령 응답 ────────────────────────────────────────────────────
            elif mtype == "response":
                cmd_id = msg.get("cmd_id")
                fut    = pending.get(cmd_id)
                if fut and not fut.done():
                    fut.set_result(msg.get("content", ""))

            # ── 비동기 이벤트 (진행률, 완료 알림 등) ─────────────────────────
            elif mtype == "event":
                gid     = msg.get("guild_id") or guild_id
                content = msg.get("content", "")
                if gid and content:
                    asyncio.create_task(_channel_send(gid, content))

            # ── 다운로드 상태 캐시 (autocomplete용) ──────────────────────────
            elif mtype == "state":
                gid = msg.get("guild_id") or guild_id
                if gid:
                    dl_state[gid] = msg.get("data", {})

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        logger.error("ws_handler error guild=%s: %s", guild_id, e)
    finally:
        if guild_id and guilds.get(guild_id, {}).get("ws") is ws:
            del guilds[guild_id]["ws"]
            logger.info("WS disconnected: guild=%s", guild_id)
            asyncio.create_task(_channel_send(
                guild_id, "⚠️ StreamSaver PC 연결이 끊어졌습니다."))


# ── Discord 봇 ───────────────────────────────────────────────────────────────

class RelayCog(commands.Cog):
    def __init__(self, b: commands.Bot):
        self.bot = b

    # ── Discord 연결 이벤트 ──────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        global bot_discord_connected
        bot_discord_connected = True
        logger.info("Bot logged in as %s", self.bot.user)
        # 이전 로컬 봇이 길드별로 등록한 커맨드 정리
        for guild in self.bot.guilds:
            self.bot.tree.clear_commands(guild=guild)
            await self.bot.tree.sync(guild=guild)
        # 글로벌 슬래시 커맨드 등록
        synced = await self.bot.tree.sync()
        logger.info("Synced %d slash commands", len(synced))
        asyncio.create_task(_push_to_all(json.dumps({
            "type": "bot_status", "bot_discord": True})))

    @commands.Cog.listener()
    async def on_disconnect(self):
        global bot_discord_connected
        bot_discord_connected = False
        logger.warning("Bot disconnected from Discord")
        asyncio.create_task(_push_to_all(json.dumps({
            "type": "bot_status", "bot_discord": False})))

    @commands.Cog.listener()
    async def on_resumed(self):
        global bot_discord_connected
        bot_discord_connected = True
        logger.info("Bot connection resumed")
        asyncio.create_task(_push_to_all(json.dumps({
            "type": "bot_status", "bot_discord": True})))

    # ── /setup ───────────────────────────────────────────────────────────────

    @app_commands.command(name="setup", description="이 채널을 StreamSaver 채널로 설정합니다")
    async def cmd_setup(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)

        if gid not in guilds:
            guilds[gid] = {}
        guilds[gid]["channel_id"] = interaction.channel_id
        _save_state()  # channel_id 영속화

        # 이미 PC가 연결돼 있으면 코드 불필요
        if _is_connected(gid):
            await interaction.response.send_message(
                "✅ 이 채널로 설정 완료!\nStreamSaver PC가 이미 연결되어 있습니다.")
            return

        # 페어링 코드 발급
        code = _gen_code()
        pair_codes[code] = {
            "guild_id": gid,
            "expires":  datetime.utcnow() + timedelta(minutes=10),
        }

        await interaction.response.send_message(
            "✅ 이 채널로 설정 완료!\n\n"
            "**PC 연결 방법:**\n"
            "1. Windows에서 StreamSaver 실행\n"
            "2. 트레이 아이콘 우클릭 → **서버 연결**\n"
            f"3. 아래 코드를 입력하세요:\n\n"
            f"```\n{code}\n```\n"
            "⏱️ 이 코드는 10분 후 만료됩니다.",
            ephemeral=True,
        )

    # ── /dl ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="dl", description="YouTube 영상 다운로드")
    @app_commands.describe(url="YouTube URL")
    async def cmd_dl(self, interaction: discord.Interaction, url: str):
        gid = str(interaction.guild_id)
        await interaction.response.defer()
        result = await _send_cmd(gid, "dl", {"url": url, "user": interaction.user.name})
        await interaction.followup.send(result)

    # ── /cancel ──────────────────────────────────────────────────────────────

    @app_commands.command(name="cancel", description="다운로드 작업 취소")
    @app_commands.describe(task_id="취소할 작업")
    async def cmd_cancel(self, interaction: discord.Interaction, task_id: int):
        gid = str(interaction.guild_id)
        await interaction.response.defer()
        result = await _send_cmd(gid, "cancel", {"task_id": task_id})
        await interaction.followup.send(result)

    @cmd_cancel.autocomplete("task_id")
    async def _cancel_ac(self, interaction: discord.Interaction, current: str):
        gid   = str(interaction.guild_id)
        state = dl_state.get(gid, {})
        choices: list[app_commands.Choice[int]] = []

        for t in state.get("active", []):
            tid = t["id"]
            if t.get("state") == "live":
                label = f"#{tid} 🔴 라이브 · {t.get('downloaded') or '?'} · {t.get('speed') or '?'}"
            else:
                prog  = t.get("progress") or 0
                label = f"#{tid} ⬇️ {prog:.0f}% · {t.get('speed') or '?'} · ETA {t.get('eta') or '?'}"
            choices.append(app_commands.Choice(name=label[:100], value=tid))

        for t in state.get("queue_list", []):
            tid   = t["id"]
            label = f"#{tid} ⏳ 대기 · {t.get('url', '')[-40:]}"
            choices.append(app_commands.Choice(name=label[:100], value=tid))

        if current:
            choices = [c for c in choices if current in str(c.value) or current in c.name]
        return choices[:25]

    # ── /waiting ─────────────────────────────────────────────────────────────

    @app_commands.command(name="waiting", description="진행 중 / 대기 목록")
    async def cmd_waiting(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        await interaction.response.defer()
        result = await _send_cmd(gid, "waiting", {})
        await interaction.followup.send(result)

    # ── /status ──────────────────────────────────────────────────────────────

    @app_commands.command(name="status", description="봇 상태 확인")
    async def cmd_status(self, interaction: discord.Interaction):
        gid       = str(interaction.guild_id)
        connected = _is_connected(gid)
        state     = dl_state.get(gid, {})
        active    = len(state.get("active", []))
        queued    = state.get("queued", 0)
        await interaction.response.send_message(
            f"**StreamSaver 상태**\n"
            f"🖥️ PC 연결: {'✅' if connected else '❌ 연결 안 됨'}\n"
            f"🤖 봇 Discord: {'✅' if bot_discord_connected else '❌'}\n"
            f"⬇️ 진행 중: {active}개\n"
            f"📋 대기: {queued}개"
        )

    # ── /login ───────────────────────────────────────────────────────────────

    @app_commands.command(name="login", description="YouTube 로그인 (멤버십 다운로드용)")
    async def cmd_login(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        await interaction.response.defer()
        result = await _send_cmd(gid, "login", {}, timeout=300.0)
        await interaction.followup.send(result)

    # ── /unarchived ──────────────────────────────────────────────────────────

    unarchived = app_commands.Group(
        name="unarchived", description="게릴라 라이브 자동 감지 관리")

    @unarchived.command(name="add", description="감시할 채널 등록")
    @app_commands.describe(
        url="YouTube 채널 URL",
        name="표시 이름",
        filter="제목 키워드 필터 (기본: unarchived)")
    async def _ua_add(self, interaction: discord.Interaction,
                      url: str, name: str = "", filter: str = "unarchived"):
        gid = str(interaction.guild_id)
        await interaction.response.defer()
        result = await _send_cmd(gid, "unarchived_add",
                                 {"url": url, "name": name, "filter": filter})
        await interaction.followup.send(result)

    @unarchived.command(name="remove", description="채널 감시 해제")
    @app_commands.describe(name="해제할 채널 이름")
    async def _ua_remove(self, interaction: discord.Interaction, name: str):
        gid = str(interaction.guild_id)
        await interaction.response.defer()
        result = await _send_cmd(gid, "unarchived_remove", {"name": name})
        await interaction.followup.send(result)

    @unarchived.command(name="list", description="감시 중인 채널 목록")
    async def _ua_list(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        await interaction.response.defer()
        result = await _send_cmd(gid, "unarchived_list", {})
        await interaction.followup.send(result)

    @unarchived.command(name="check", description="즉시 전체 채널 점검")
    async def _ua_check(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        await interaction.response.defer()
        result = await _send_cmd(gid, "unarchived_check", {})
        await interaction.followup.send(result)

    # ── /help ────────────────────────────────────────────────────────────────

    @app_commands.command(name="help", description="명령어 안내")
    async def cmd_help(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "**StreamSaver 명령어**\n"
            "`/setup` — 이 채널 설정 + PC 연결 코드 발급\n"
            "`/dl url` — YouTube 다운로드\n"
            "`/cancel` — 작업 취소 (자동완성)\n"
            "`/waiting` — 진행 중 / 대기 목록\n"
            "`/status` — 연결 상태 확인\n"
            "`/login` — YouTube 로그인 (멤버십용)\n"
            "`/unarchived add url 이름` — 게릴라 라이브 감지 등록\n"
            "`/unarchived remove 이름` — 감지 해제\n"
            "`/unarchived list` — 감지 목록\n"
            "`/unarchived check` — 즉시 점검",
            ephemeral=True,
        )


class RelayBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix=(), intents=intents)

    async def setup_hook(self):
        await self.add_cog(RelayCog(self))


# ── 진입점 ───────────────────────────────────────────────────────────────────

async def main():
    global bot
    _load_state()
    bot = RelayBot()

    ws_server = await websockets.serve(
        ws_handler, "0.0.0.0", WS_PORT,
        ping_interval=30,
        ping_timeout=20,
    )
    logger.info("WebSocket server listening on port %d", WS_PORT)

    asyncio.create_task(_heartbeat_loop())

    async with bot:
        await bot.start(DISCORD_TOKEN)

    ws_server.close()


if __name__ == "__main__":
    asyncio.run(main())
