import json
import logging
import threading
import urllib.request
from typing import Optional
from urllib.error import URLError

import config

logger = logging.getLogger("StreamSaver.Updater")

_API_URL = f"https://api.github.com/repos/{config.GITHUB_REPO}/releases/latest"


def _parse_version(tag: str) -> tuple:
    """'v1.2.3' 또는 '1.2.3' → (1, 2, 3)"""
    tag = tag.lstrip("v")
    try:
        return tuple(int(x) for x in tag.split("."))
    except ValueError:
        return (0,)


def check_update() -> Optional[dict]:
    """
    최신 릴리즈를 확인해 업데이트가 있으면 dict 반환, 없으면 None.
    반환: {"version": "1.2.0", "url": "https://...", "notes": "..."}
    """
    try:
        req = urllib.request.Request(
            _API_URL,
            headers={"User-Agent": f"StreamSaver/{config.APP_VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        latest_tag = data.get("tag_name", "")
        if not latest_tag:
            return None

        if _parse_version(latest_tag) <= _parse_version(config.APP_VERSION):
            logger.info("Up to date (%s)", config.APP_VERSION)
            return None

        download_url = ""
        for asset in data.get("assets", []):
            if asset["name"].endswith(".exe"):
                download_url = asset["browser_download_url"]
                break
        if not download_url:
            download_url = data.get("html_url", "")

        notes = (data.get("body") or "").strip()[:200]
        logger.info("Update available: %s → %s", config.APP_VERSION, latest_tag)
        return {
            "version":  latest_tag.lstrip("v"),
            "url":      download_url,
            "notes":    notes,
        }

    except URLError as e:
        logger.debug("Update check failed (network): %s", e)
    except Exception as e:
        logger.debug("Update check error: %s", e)
    return None


def check_update_async(callback):
    """백그라운드 스레드에서 체크 후 결과를 callback(info_or_none) 으로 전달"""
    def _run():
        info = check_update()
        callback(info)
    threading.Thread(target=_run, daemon=True, name="updater").start()
