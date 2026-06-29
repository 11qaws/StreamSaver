import asyncio
import json
import logging
import os
import subprocess

import config

logger = logging.getLogger("StreamSaver.Watcher")
_NW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class StreamWatcher:
    def __init__(self, download_manager, cookie_manager):
        self.dl = download_manager
        self.cm = cookie_manager
        self._channels: dict = {}   # url → {"name": str, "title_filter": str}
        self._seen: set = set()     # 이미 큐에 넣은 video ID
        self._notify_cb = None
        self._task = None
        self._file = os.path.join(config.BASE_DIR, "watch_channels.json")
        self._load()

    # ── 영속성 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_handle(url: str) -> str:
        """URL에서 @handle 추출. 예: https://www.youtube.com/@OuroKronii → @OuroKronii"""
        for part in url.rstrip("/").split("/"):
            if part.startswith("@"):
                return part
        return ""

    @staticmethod
    def display_name(info: dict) -> str:
        """트레이·Discord 공통 표시 이름: '이름 (@handle)' 또는 '@handle'"""
        name   = info.get("name", "")
        handle = info.get("handle", "")
        if handle and name and name != handle:
            return f"{name} ({handle})"
        return handle or name

    def _load(self):
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for ch in data.get("channels", []):
                handle = ch.get("handle", "") or self._extract_handle(ch["url"])
                self._channels[ch["url"]] = {
                    "name":         ch.get("name", handle or ch["url"]),
                    "handle":       handle,
                    "title_filter": ch.get("title_filter", ""),
                }
            logger.info("Loaded %d watched channels", len(self._channels))
        except Exception as e:
            logger.error("watch_channels.json load error: %s", e)

    def _save(self):
        data = {"channels": [
            {"url": url, "name": info["name"], "handle": info.get("handle", ""),
             "title_filter": info["title_filter"]}
            for url, info in self._channels.items()
        ]}
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("watch_channels.json save error: %s", e)

    # ── 채널 관리 ──────────────────────────────────────────────────────────────

    def add(self, url: str, name: str = "", title_filter: str = "") -> str:
        url    = url.rstrip("/")
        handle = self._extract_handle(url)
        if not name:
            name = handle or url.split("/")[-1]
        self._channels[url] = {"name": name, "handle": handle, "title_filter": title_filter}
        self._save()
        return self.display_name(self._channels[url])

    def remove(self, url: str) -> bool:
        url = url.rstrip("/")
        if url in self._channels:
            del self._channels[url]
            self._save()
            return True
        for u, info in list(self._channels.items()):
            if info["name"].lower() == url.lower():
                del self._channels[u]
                self._save()
                return True
        return False

    def list_channels(self):
        return list(self._channels.items())

    # ── 알림 콜백 ─────────────────────────────────────────────────────────────

    def set_notify(self, cb):
        """cb(message: str) — 스트림 감지 시 동기 함수로 호출됨 (스레드 컨텍스트)"""
        self._notify_cb = cb

    # ── 폴링 루프 ─────────────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop):
        self._task = loop.create_task(self._poll_loop())
        logger.info("StreamWatcher started (poll interval=%ds)", config.WATCH_POLL_INTERVAL)

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _poll_loop(self):
        while True:
            if self._channels:
                await self._check_all()
            await asyncio.sleep(config.WATCH_POLL_INTERVAL)

    async def _check_all(self):
        loop = asyncio.get_event_loop()
        for url, info in list(self._channels.items()):
            try:
                await loop.run_in_executor(None, self._check_channel, url, info)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("check_channel error [%s]: %s", url, e)

    def _check_channel(self, channel_url: str, ch_info: dict):
        live_url = channel_url.rstrip("/") + "/live"

        cookie_args = []
        if self.cm and self.cm.cookie_valid and os.path.exists(config.COOKIE_FILE):
            cookie_args = ["--cookies", config.COOKIE_FILE]

        js_args = []
        if config.NODE_JS:
            js_args = ["--js-runtimes", f"node:{config.NODE_JS}"]

        cmd = [config.YT_DLP, "-J", "--no-warnings", "--no-config",
               "--no-playlist", *js_args, *cookie_args, live_url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                               creationflags=_NW)
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp timeout checking %s", channel_url)
            return
        except Exception as e:
            logger.error("yt-dlp error checking %s: %s", channel_url, e)
            return

        if r.returncode != 0 or not r.stdout.strip():
            return

        try:
            meta = json.loads(r.stdout.strip().split("\n")[0])
        except json.JSONDecodeError:
            return

        if meta.get("live_status") != "is_live":
            return

        vid_id = meta.get("id", "")
        if not vid_id or vid_id in self._seen:
            return

        title = meta.get("title", "")
        title_filter = ch_info.get("title_filter", "").lower().strip()
        if title_filter and title_filter not in title.lower():
            logger.debug("Skipping '%s' (filter='%s')", title, title_filter)
            return

        channel_name = ch_info["name"]
        logger.info("LIVE DETECTED [%s] '%s' (id=%s)", channel_name, title, vid_id)

        self._seen.add(vid_id)
        video_url = f"https://www.youtube.com/watch?v={vid_id}"
        self.dl.enqueue(video_url, f"AutoWatcher({channel_name})")

        if self._notify_cb:
            try:
                self._notify_cb(
                    f"🔴 게릴라 라이브 감지!\n"
                    f"**{channel_name}**: {title}\n"
                    f"자동 녹화 시작됨 → {video_url}"
                )
            except Exception as e:
                logger.error("notify_cb error: %s", e)

    # ── 즉시 점검 (명령어용) ──────────────────────────────────────────────────

    async def check_now(self):
        if not self._channels:
            return "감시 중인 채널 없음"
        await self._check_all()
        return f"{len(self._channels)}개 채널 점검 완료"
