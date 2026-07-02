"""
StreamSaver Relay Server ??WebSocket ?�브 ?�용
Discord 봇�? discord_bot.py 별도 ?�로?�스�?분리.
???�로?�스??localhost:8766 IPC ?�켓?�로 ?�신.

?�키?�처:
  discord_bot.py  <--IPC:8766-->  server.py  <--WS:8765-->  PC Client

WebSocket ?�브(???�일)??Discord ?�존???�이 asyncio ?�벤??루프�??�용.
Discord �??�로?�스가 ?�어붙어??WebSocket ?�버??무영??

?�중 ?�용??격리:
- 모든 ?�우?��? guild_id 기�? ?�전 격리
- 공유 루프?�서 개별 ?�류가 ?�체???�향 ?�도�?try/except 격리
- 공유 dict ?�회 ??list() ?�냅???�용

보안/?�정??
- WebSocket ?�결 ???�한 (MAX_WS_CONNECTIONS)
- ?�신 메시지 ?�기 ?�한 (max_size=1MiB)
- pair_code 만료 주기???�리 (_cleanup_loop)
- URL ?�킴·길이 검�?- state.json ?�자???�기 (tmp ??replace)
- asyncio ?�벤??루프 ?�결 감�? (OS ?�레??watchdog)
"""
import asyncio
import json
import logging
import os
import random
import signal
import string
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

import psutil
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Relay")

WS_PORT        = int(os.getenv("WS_PORT", "8765"))
IPC_PORT       = int(os.getenv("IPC_PORT", "8766"))
WS_SECRET      = os.getenv("WS_SECRET", "")
SERVER_VERSION = "1.2.6"

MAX_WS_CONNECTIONS = 100

# ?�?� ?�벤??루프 ?�결 감�? watchdog ?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�
# asyncio ?�벤??루프가 멈추�?메모�?watchdog???�행?��? ?�아 무한 무응???�태가 ??
# OS ?�레?�에??루프 ???�?�스?�프�?감시??30�??�상 ?�이 ?�으�?SIGKILL�?강제 종료.
_loop_last_tick: float = 0.0
_LOOP_FREEZE_SEC = 120

async def _loop_heartbeat():
    global _loop_last_tick
    while True:
        _loop_last_tick = time.monotonic()
        await asyncio.sleep(5)

def _freeze_watchdog_thread():
    time.sleep(15)
    while True:
        time.sleep(5)
        if _loop_last_tick > 0 and time.monotonic() - _loop_last_tick > _LOOP_FREEZE_SEC:
            logger.critical(
                "asyncio event loop frozen for %.0fs ??SIGKILL",
                time.monotonic() - _loop_last_tick,
            )
            os.kill(os.getpid(), signal.SIGKILL)

# ?�?� ?�태 ?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# guild_id ??{"channel_id": int, "ws": websockets.WebSocketServerProtocol}
guilds: dict[str, dict] = {}

# pair_code ??{"guild_id": str, "expires": datetime}
pair_codes: dict[str, dict] = {}

# cmd_id ??asyncio.Future
pending: dict[str, asyncio.Future] = {}

# guild_id ???�운로드 ?�태 캐시 (discord_bot autocomplete??
dl_state: dict[str, dict] = {}

bot_discord_connected: bool = False
_active_connections: int = 0

# ?�?� IPC (discord_bot ??server) ?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�
#
# ?�재: 1:1 ?�일 ?�결 ??discord_bot.py ?�나�?127.0.0.1:8766?�로 ?�결.
#
# ?�장 ?�션 A ??같�? VPS ?�에???�비??추�? (카카?�톡 ?�림, ?�훅 ??:
#   _ipc_writer ??_ipc_clients: dict[StreamWriter, role] �?교체.
#   _ipc_send() ??_ipc_broadcast() / _ipc_send_to_role(role) 분리.
#   �??�비?�는 ?�결 직후 {"t":"register","role":"..."} �??�신???�별.
#
# ?�장 ?�션 B ???�비?��? ?�른 ?�버�?분산??경우 Redis pub/sub?�로 교체:
#   server.py ??redis.publish("relay:event", json.dumps(msg))
#   �??�비????redis.subscribe("relay:event")
#   ?�점: ?�로?�스 ?�치 무�?, ?�평 ?�장, 메시지 ?�속??Stream ?�용 ??
#   ?�점: Redis ?�스?�스 관�??�요, ?�재 VPS ?�일 구성?�선 ?�버?��??�어�?
_ipc_writer: Optional[asyncio.StreamWriter] = None


async def _ipc_send(msg: dict):
    """discord_bot??JSON 메시지 ?�송 (fire-and-forget)."""
    w = _ipc_writer
    if not w:
        return
    try:
        w.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await w.drain()
    except Exception as e:
        logger.debug("IPC send error: %s", e)


async def _ipc_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """discord_bot IPC ?�결 처리."""
    global _ipc_writer, bot_discord_connected
    _ipc_writer = writer
    addr = writer.get_extra_info("peername")
    logger.info("Bot IPC connected from %s", addr)

    try:
        async for raw in reader:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = msg.get("t")

            if t == "bot_up":
                bot_discord_connected = True
                asyncio.create_task(_push_to_all(json.dumps(
                    {"type": "bot_status", "bot_discord": True})))
                logger.info("Discord bot connected")

            elif t == "bot_down":
                bot_discord_connected = False
                asyncio.create_task(_push_to_all(json.dumps(
                    {"type": "bot_status", "bot_discord": False})))
                logger.info("Discord bot disconnected from Discord")

            elif t == "cmd":
                gid     = msg.get("gid", "")
                cmd     = msg.get("cmd", "")
                args    = msg.get("args", {})
                req_id  = msg.get("id", "")
                timeout = float(msg.get("timeout", 12))
                asyncio.create_task(_handle_bot_cmd(gid, cmd, args, req_id, timeout))

            elif t == "setup":
                gid    = msg.get("gid", "")
                cid    = msg.get("cid")
                req_id = msg.get("id", "")
                asyncio.create_task(_handle_setup(gid, cid, req_id))

            elif t == "get_state":
                gid    = msg.get("gid", "")
                req_id = msg.get("id", "")
                state  = dl_state.get(gid, {})
                connected = _is_connected(gid)
                await _ipc_send({"t": "state_resp", "id": req_id,
                                 "gid": gid, "connected": connected, "data": state})

    except Exception as e:
        logger.warning("IPC handler error: %s", e)
    finally:
        if _ipc_writer is writer:
            _ipc_writer = None
        bot_discord_connected = False
        asyncio.create_task(_push_to_all(json.dumps(
            {"type": "bot_status", "bot_discord": False})))
        logger.info("Bot IPC disconnected")


async def _handle_bot_cmd(gid: str, cmd: str, args: dict, req_id: str, timeout: float):
    result = await _send_cmd(gid, cmd, args, timeout)
    await _ipc_send({"t": "resp", "id": req_id, "msg": result})


async def _handle_setup(gid: str, cid: Optional[int], req_id: str):
    if gid not in guilds:
        guilds[gid] = {}
    if cid:
        guilds[gid]["channel_id"] = cid
        _save_state()

    if _is_connected(gid):
        await _ipc_send({"t": "setup_resp", "id": req_id, "connected": True})
        return

    code = _gen_code()
    pair_codes[code] = {
        "guild_id": gid,
        "expires":  datetime.utcnow() + timedelta(minutes=10),
    }
    await _ipc_send({"t": "setup_resp", "id": req_id, "connected": False, "code": code})


# ?�?� ?�태 ?�속???�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�

def _load_state():
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
    data = {
        "guilds": {
            gid: {"channel_id": info.get("channel_id")}
            for gid, info in guilds.items()
            if info.get("channel_id")
        }
    }
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.warning("State save error: %s", e)
        try:
            os.remove(tmp)
        except Exception:
            pass


# ?�?� ?�틸 ?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�

def _gen_code() -> str:
    return (
        "".join(random.choices(string.ascii_uppercase, k=3))
        + "-"
        + "".join(random.choices(string.digits, k=3))
    )


def _is_connected(guild_id: str) -> bool:
    return guild_id in guilds and "ws" in guilds[guild_id]


async def _push_to_all(payload: str):
    for gid, info in list(guilds.items()):
        ws = info.get("ws")
        if ws:
            try:
                await ws.send(payload)
            except Exception as e:
                logger.debug("push_to_all guild=%s error: %s", gid, e)


async def _channel_send(guild_id: str, content: str):
    """Discord 채널�?메시지 ?�송 ??IPC�?discord_bot???�임."""
    if guild_id not in guilds:
        return
    ch_id = guilds[guild_id].get("channel_id")
    if not ch_id:
        return
    await _ipc_send({"t": "send", "cid": ch_id, "msg": content})


async def _heartbeat_loop():
    while True:
        try:
            await asyncio.sleep(45)
            connected_count = sum(1 for g in guilds.values() if g.get("ws"))
            if connected_count == 0:
                continue
            payload = json.dumps({
                "type":        "heartbeat",
                "bot_discord": bot_discord_connected,
            })
            await _push_to_all(payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("heartbeat_loop error: %s", e)


async def _cleanup_loop():
    while True:
        try:
            await asyncio.sleep(300)
            now = datetime.utcnow()
            expired = [c for c, v in list(pair_codes.items()) if now > v["expires"]]
            for c in expired:
                pair_codes.pop(c, None)
            if expired:
                logger.debug("Cleaned %d expired pair codes", len(expired))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("cleanup_loop error: %s", e)


_MEMORY_LIMIT_MB = int(os.getenv("MEMORY_LIMIT_MB", "180"))

async def _memory_watchdog():
    proc = psutil.Process()
    while True:
        try:
            await asyncio.sleep(60)
            rss_mb = proc.memory_info().rss / 1024 / 1024
            logger.debug("Memory: %.1f MB / %d MB limit", rss_mb, _MEMORY_LIMIT_MB)
            if rss_mb > _MEMORY_LIMIT_MB:
                logger.warning(
                    "Memory limit exceeded (%.1f MB > %d MB) ??restarting",
                    rss_mb, _MEMORY_LIMIT_MB,
                )
                sys.exit(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("memory_watchdog error: %s", e)


# ?�?� PC 명령 ?�달 ?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�

async def _send_cmd(guild_id: str, cmd: str, args: dict, timeout: float = 12.0) -> str:
    if not _is_connected(guild_id):
        return "??StreamSaver PC가 ?�결?�어 ?��? ?�습?�다. ?�로그램???�행 중인지 ?�인?�세??"

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
        return "?�️ ?�답 ?�음 ??PC가 켜져 ?�고 StreamSaver가 ?�행 중인지 ?�인?�세??"
    except Exception as e:
        logger.error("send_cmd error guild=%s: %s", guild_id, e)
        return f"???�류: {e}"
    finally:
        pending.pop(cmd_id, None)


# ?�?� WebSocket ?�버 ?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�

async def ws_handler(ws):
    global _active_connections

    if _active_connections >= MAX_WS_CONNECTIONS:
        logger.warning("WS rejected: max connections (%d) reached", MAX_WS_CONNECTIONS)
        await ws.close(1008, "Too many connections")
        return

    _active_connections += 1
    guild_id: Optional[str] = None
    addr = ws.remote_address
    logger.info("WS connected: %s (total: %d)", addr, _active_connections)

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")

            # ?�?� ?�연�??�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�
            if mtype == "reconnect":
                secret = msg.get("secret", "")
                if WS_SECRET and secret != WS_SECRET:
                    await ws.send(json.dumps({"type": "error", "message": "?�증 ?�패"}))
                    continue
                gid = msg.get("guild_id", "")
                if not gid:
                    await ws.send(json.dumps({"type": "error", "message": "guild_id ?�음"}))
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
                asyncio.create_task(_channel_send(guild_id, "??StreamSaver PC가 ?�연결되?�습?�다."))

            # ?�?� ?�어�??�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�
            elif mtype == "pair":
                secret = msg.get("secret", "")
                if WS_SECRET and secret != WS_SECRET:
                    await ws.send(json.dumps({"type": "error", "message": "?�증 ?�패"}))
                    continue

                code = msg.get("code", "").strip().upper()
                entry = pair_codes.get(code)
                if not entry or datetime.utcnow() > entry["expires"]:
                    await ws.send(json.dumps({"type": "error", "message": "?�효?��? ?�거??만료??코드?�니??}))
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
                asyncio.create_task(_channel_send(guild_id, "??StreamSaver PC가 ?�결?�었?�니??"))

            # ?�?� 명령 ?�답 ?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�
            elif mtype == "response":
                cmd_id = msg.get("cmd_id")
                fut    = pending.get(cmd_id)
                if fut and not fut.done():
                    fut.set_result(msg.get("content", ""))

            # ?�?� 비동�??�벤???�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�
            elif mtype == "event":
                gid     = msg.get("guild_id") or guild_id
                content = msg.get("content", "")
                if gid and content:
                    asyncio.create_task(_channel_send(gid, content))

            # ?�?� ?�운로드 ?�태 캐시 ?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�
            elif mtype == "state":
                gid = msg.get("guild_id") or guild_id
                if gid:
                    dl_state[gid] = msg.get("data", {})
                    asyncio.create_task(_ipc_send({
                        "t": "state_push", "gid": gid, "data": dl_state[gid]}))

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        logger.error("ws_handler error guild=%s: %s", guild_id, e)
    finally:
        _active_connections -= 1
        if guild_id and guilds.get(guild_id, {}).get("ws") is ws:
            del guilds[guild_id]["ws"]
            logger.info("WS disconnected: guild=%s (total: %d)", guild_id, _active_connections)
            asyncio.create_task(_channel_send(guild_id, "?�️ StreamSaver PC ?�결???�어졌습?�다."))


# ?�?� 진입???�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�?�

async def main():
    _load_state()

    # asyncio ?�벤??루프 ?�결 감�? (OS ?�레??
    threading.Thread(target=_freeze_watchdog_thread, daemon=True, name="freeze-wd").start()

    # WebSocket ?�버
    ws_server = await websockets.serve(
        ws_handler, "0.0.0.0", WS_PORT,
        ping_interval=30,
        ping_timeout=20,
        max_size=1_048_576,
    )
    logger.info("WebSocket server listening on port %d", WS_PORT)

    # IPC ?�버 (discord_bot ?�결 ?��?
    ipc_server = await asyncio.start_server(
        _ipc_handler, "127.0.0.1", IPC_PORT)
    logger.info("IPC server listening on port %d (localhost only)", IPC_PORT)

    asyncio.create_task(_loop_heartbeat())
    asyncio.create_task(_heartbeat_loop())
    asyncio.create_task(_cleanup_loop())
    asyncio.create_task(_memory_watchdog())

    async with ws_server, ipc_server:
        await asyncio.Future()   # ?�구 ?�행


if __name__ == "__main__":
    asyncio.run(main())

