"""
StreamSaver Discord ë´???server.py?€ ë³„ë„ ?„ë¡œ?¸ìŠ¤
server.py IPC(localhost:8766)???°ê²°??ëª…ë ¹??ì£¼ê³ ë°›ìŒ.

systemd: streamsaver-bot.service (Restart=always)
Discord ?¬ì—°ê²°ì´ ???„ë¡œ?¸ìŠ¤ë¥??¼ì–´ë¶™í???server.py ?ëŠ” ?í–¥ ?†ìŒ.

IPC ë©”ì‹œì§€ ?•ì‹ (newline-delimited JSON):
  Bot ??Hub: {"t":"bot_up"} / {"t":"bot_down"} / {"t":"cmd",...} / {"t":"setup",...}
  Hub ??Bot: {"t":"send","cid":"...","msg":"..."} / {"t":"resp","id":"...","msg":"..."} /
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
BOT_VERSION = "1.2.6"

# cmd cooldown ì¶”ì 
_cooldown: dict[str, float] = {}
COOLDOWN_SEC = 3.0

# IPC ?‘ë‹µ ?€ê¸?futures
_pending: dict[str, asyncio.Future] = {}

# ?¤ìš´ë¡œë“œ ?íƒœ ìºì‹œ (guild_id ??data)
_dl_state: dict[str, dict] = {}

# ?€?€ IPC ?´ë¼?´ì–¸???€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€

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
            logger.warning("IPC connect failed: %s ??retry in %ds", e, delay)
        except Exception as e:
            logger.error("IPC error: %s ??retry in %ds", e, delay)
        finally:
            _writer = None
            try:
                if "writer" in dir():
                    writer.close()
            except Exception:
                pass

        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


# ?€?€ Discord ë´??€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€

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
        # IPC???°ê²° ?Œë¦¼?€ connect_ipc()?ì„œ ?´ë? ?˜í–‰

    @commands.Cog.listener()
    async def on_disconnect(self):
        await ipc_send({"t": "bot_down"})

    @commands.Cog.listener()
    async def on_resumed(self):
        await ipc_send({"t": "bot_up"})

    # ?€?€ Slash: /setup ?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€
    @app_commands.command(name="setup", description="??ì±„ë„??StreamSaverë¥??°ê²°?©ë‹ˆ??)
    @app_commands.default_permissions(administrator=True)
    async def slash_setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        gid = str(interaction.guild_id)
        cid = interaction.channel_id

        resp = await ipc_call({"t": "setup", "gid": gid, "cid": cid})
        if resp is None:
            await interaction.followup.send("???ˆë¸Œ ?œë²„ ?‘ë‹µ ?†ìŒ", ephemeral=True)
            return

        if resp.get("connected"):
            await interaction.followup.send("???´ë? PCê°€ ?°ê²°?˜ì–´ ?ˆìŠµ?ˆë‹¤!", ephemeral=True)
        else:
            code = resp.get("code", "???")
            await interaction.followup.send(
                f"?”— StreamSaver PC???€?œë³´????**?œë²„ ?°ê²°** ?ì„œ ì½”ë“œë¥??…ë ¥?˜ì„¸??\n"
                f"## `{code}`\n"
                f"ì½”ë“œ??10ë¶„ê°„ ? íš¨?©ë‹ˆ??",
                ephemeral=True,
            )

    # ?€?€ Slash: /download ?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€
    @app_commands.command(name="download", description="YouTube ?ìƒ???¤ìš´ë¡œë“œ?©ë‹ˆ??)
    @app_commands.describe(url="YouTube URL", quality="?”ì§ˆ ? íƒ (ê¸°ë³¸: best)")
    @app_commands.choices(quality=[
        app_commands.Choice(name="ìµœê³ ?”ì§ˆ (best)", value="best"),
        app_commands.Choice(name="1080p",          value="1080"),
        app_commands.Choice(name="720p",           value="720"),
        app_commands.Choice(name="480p",           value="480"),
        app_commands.Choice(name="?¤ë””?¤ë§Œ (mp3)", value="audio"),
    ])
    async def slash_dl(self, interaction: discord.Interaction, url: str,
                       quality: str = "best"):
        gid = str(interaction.guild_id)
        if not _cooldown_ok(gid):
            await interaction.response.send_message(
                f"???ˆë¬´ ë¹ ë¦…?ˆë‹¤. {COOLDOWN_SEC:.0f}ì´????¤ì‹œ ?œë„?˜ì„¸??", ephemeral=True)
            return
        if not _validate_url(url):
            await interaction.response.send_message("??? íš¨?˜ì? ?Šì? URL?…ë‹ˆ??", ephemeral=True)
            return

        await interaction.response.defer()
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "dl",
             "args": {"url": url, "quality": quality}},
            timeout=15,
        )
        if resp is None:
            await interaction.followup.send(
                "?±ï¸ ?‘ë‹µ ?†ìŒ ??PCê°€ ì¼œì ¸ ?ˆê³  StreamSaverê°€ ?¤í–‰ ì¤‘ì¸ì§€ ?•ì¸?˜ì„¸??")
        else:
            await interaction.followup.send(resp.get("msg", "??))

    # ?€?€ Slash: /status ?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€
    @app_commands.command(name="status", description="?„ì¬ ?¤ìš´ë¡œë“œ ?íƒœë¥??•ì¸?©ë‹ˆ??)
    async def slash_status(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        if not _cooldown_ok(gid):
            await interaction.response.send_message(
                f"??{COOLDOWN_SEC:.0f}ì´????¤ì‹œ ?œë„?˜ì„¸??", ephemeral=True)
            return

        await interaction.response.defer()
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "status", "args": {}},
            timeout=12,
        )
        if resp is None:
            await interaction.followup.send(
                "?±ï¸ ?‘ë‹µ ?†ìŒ ??StreamSaver PCê°€ ?°ê²°?˜ì–´ ?ˆëŠ”ì§€ ?•ì¸?˜ì„¸??")
        else:
            await interaction.followup.send(resp.get("msg", "??))

    # ?€?€ Slash: /cancel ?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€
    @app_commands.command(name="cancel", description="ì§„í–‰ ì¤‘ì¸ ?¤ìš´ë¡œë“œë¥?ì·¨ì†Œ?©ë‹ˆ??)
    @app_commands.describe(task_id="ì·¨ì†Œ???‘ì—… ID (?†ìœ¼ë©??„ì²´ ì·¨ì†Œ)")
    async def slash_cancel(self, interaction: discord.Interaction,
                           task_id: Optional[str] = None):
        gid = str(interaction.guild_id)
        if not _cooldown_ok(gid):
            await interaction.response.send_message(
                f"??{COOLDOWN_SEC:.0f}ì´????¤ì‹œ ?œë„?˜ì„¸??", ephemeral=True)
            return

        await interaction.response.defer()
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "cancel",
             "args": {"task_id": task_id}},
            timeout=12,
        )
        if resp is None:
            await interaction.followup.send("?±ï¸ ?‘ë‹µ ?†ìŒ")
        else:
            await interaction.followup.send(resp.get("msg", "??))

    # ?€?€ Slash: /login ?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€
    @app_commands.command(name="login", description="Edge ë¸Œë¼?°ì?ë¡?YouTube ë¡œê·¸?¸ì„ ?œì‘?©ë‹ˆ??)
    async def slash_login(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        if not _cooldown_ok(gid):
            await interaction.response.send_message(
                f"??{COOLDOWN_SEC:.0f}ì´????¤ì‹œ ?œë„?˜ì„¸??", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "login", "args": {}},
            timeout=12,
        )
        if resp is None:
            await interaction.followup.send("?±ï¸ ?‘ë‹µ ?†ìŒ", ephemeral=True)
        else:
            await interaction.followup.send(resp.get("msg", "??), ephemeral=True)

    # ?€?€ Slash: /restart ?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€
    @app_commands.command(name="restart", description="StreamSaverë¥??¬ì‹œ?‘í•©?ˆë‹¤")
    @app_commands.default_permissions(administrator=True)
    async def slash_restart(self, interaction: discord.Interaction):
        gid = str(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "restart", "args": {}},
            timeout=12,
        )
        if resp is None:
            await interaction.followup.send("?±ï¸ ?‘ë‹µ ?†ìŒ", ephemeral=True)
        else:
            await interaction.followup.send(resp.get("msg", "??), ephemeral=True)

    # ?€?€ Slash: /help ?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€
    @app_commands.command(name="help", description="StreamSaver ?¬ìš©ë²•ì„ ?ˆë‚´?©ë‹ˆ??)
    async def slash_help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="StreamSaver ?„ì?ë§?,
            description=f"ë²„ì „ {BOT_VERSION}",
            color=0x3B82F6,
        )
        embed.add_field(name="/setup",   value="??ì±„ë„??PC ?°ê²°", inline=False)
        embed.add_field(name="/download [url]", value="YouTube ?¤ìš´ë¡œë“œ ?œì‘", inline=False)
        embed.add_field(name="/status",  value="?¤ìš´ë¡œë“œ ?„í™© ?•ì¸", inline=False)
        embed.add_field(name="/cancel",  value="?¤ìš´ë¡œë“œ ì·¨ì†Œ", inline=False)
        embed.add_field(name="/login",   value="YouTube ë¡œê·¸??ê°±ì‹ ", inline=False)
        embed.add_field(name="/restart", value="???¬ì‹œ??(ê´€ë¦¬ì)", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ?€?€ ?ˆê±°??!ëª…ë ¹??(?¸í™˜?? ?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€
    @commands.command(name="dl")
    async def cmd_dl(self, ctx: commands.Context, *, url: str = ""):
        if not url or not _validate_url(url):
            await ctx.reply("???¬ë°”ë¥?URL???…ë ¥?˜ì„¸??")
            return
        gid = str(ctx.guild.id)
        if not _cooldown_ok(gid):
            await ctx.reply(f"??{COOLDOWN_SEC:.0f}ì´????¤ì‹œ ?œë„?˜ì„¸??")
            return
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "dl",
             "args": {"url": url, "quality": "best"}},
            timeout=15,
        )
        await ctx.reply(resp.get("msg", "??) if resp else "?±ï¸ ?‘ë‹µ ?†ìŒ")

    @commands.command(name="?íƒœ")
    async def cmd_status(self, ctx: commands.Context):
        gid = str(ctx.guild.id)
        if not _cooldown_ok(gid):
            await ctx.reply(f"??{COOLDOWN_SEC:.0f}ì´????¤ì‹œ ?œë„?˜ì„¸??")
            return
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "status", "args": {}}, timeout=12)
        await ctx.reply(resp.get("msg", "??) if resp else "?±ï¸ ?‘ë‹µ ?†ìŒ")

    @commands.command(name="ì·¨ì†Œ")
    async def cmd_cancel(self, ctx: commands.Context, task_id: str = ""):
        gid = str(ctx.guild.id)
        if not _cooldown_ok(gid):
            await ctx.reply(f"??{COOLDOWN_SEC:.0f}ì´????¤ì‹œ ?œë„?˜ì„¸??")
            return
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "cancel",
             "args": {"task_id": task_id or None}},
            timeout=12,
        )
        await ctx.reply(resp.get("msg", "??) if resp else "?±ï¸ ?‘ë‹µ ?†ìŒ")

    @commands.command(name="ë¡œê·¸??)
    async def cmd_login(self, ctx: commands.Context):
        gid = str(ctx.guild.id)
        if not _cooldown_ok(gid):
            await ctx.reply(f"??{COOLDOWN_SEC:.0f}ì´????¤ì‹œ ?œë„?˜ì„¸??")
            return
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "login", "args": {}}, timeout=12)
        await ctx.reply(resp.get("msg", "??) if resp else "?±ï¸ ?‘ë‹µ ?†ìŒ")

    @commands.command(name="?¬ì‹œ??)
    @commands.has_permissions(administrator=True)
    async def cmd_restart(self, ctx: commands.Context):
        gid = str(ctx.guild.id)
        resp = await ipc_call(
            {"t": "cmd", "gid": gid, "cmd": "restart", "args": {}}, timeout=12)
        await ctx.reply(resp.get("msg", "??) if resp else "?±ï¸ ?‘ë‹µ ?†ìŒ")


# ?€?€ ì§„ì…???€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€?€

async def main():
    bot = StreamBot()

    # IPC ?°ê²° (ë°±ê·¸?¼ìš´??
    asyncio.create_task(connect_ipc())

    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())

