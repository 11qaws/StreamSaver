import json
import logging
import os
import tempfile
import threading
import time
import urllib.request
from typing import Callable, Optional
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

        notes = (data.get("body") or "").strip()
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


def check_update_async(callback, delay: float = 0):
    """백그라운드 스레드에서 체크 후 결과를 callback(info_or_none) 으로 전달.
    delay > 0 이면 해당 초만큼 대기 후 체크 (앱 시작 부하 분산용)."""
    def _run():
        if delay > 0:
            time.sleep(delay)
        info = check_update()
        callback(info)
    threading.Thread(target=_run, daemon=True, name="updater").start()


def check_update_loop(callback, interval: int = 21600):
    """interval 초마다 주기적으로 체크 (기본 6시간). 새 버전 발견 시 callback 호출."""
    def _loop():
        while True:
            time.sleep(interval)
            try:
                info = check_update()
                if info:
                    callback(info)
            except Exception as e:
                logger.debug("Periodic update check error: %s", e)
    threading.Thread(target=_loop, daemon=True, name="updater-loop").start()


def download_update(url: str, progress_cb: Optional[Callable[[int], None]] = None) -> str:
    """
    새 인스톨러를 임시 폴더에 다운로드하고 경로를 반환.
    progress_cb(pct) — 0~100 진행률 콜백 (선택)
    """
    tmp_dir  = tempfile.mkdtemp(prefix="streamsaver_upd_")
    filename = url.split("/")[-1] or "StreamSaver_Setup.exe"
    dest     = os.path.join(tmp_dir, filename)

    req = urllib.request.Request(
        url, headers={"User-Agent": f"StreamSaver/{config.APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total      = int(resp.headers.get("Content-Length", 0) or 0)
        downloaded = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total:
                    progress_cb(int(downloaded * 100 / total))

    if progress_cb:
        progress_cb(100)
    return dest


def install_update(installer_path: str):
    """인스톨러를 /VERYSILENT 모드로 실행 (UI 없음)."""
    import subprocess
    _NW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [installer_path, "/VERYSILENT", "/NORESTART"],
        creationflags=_NW,
    )
