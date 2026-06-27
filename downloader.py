import subprocess
import threading
import os
import re
import json
import time
import logging
from queue import Queue
from enum import Enum
from datetime import datetime

import config

logger = logging.getLogger("StreamSaver.Downloader")


class TaskStatus(Enum):
    QUEUED = "queued"
    GETTING_INFO = "getting_info"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DownloadTask:
    def __init__(self, url, requested_by):
        self.url = url
        self.requested_by = requested_by
        self.status = TaskStatus.QUEUED
        self.progress = 0.0
        self.speed = ""
        self.eta = ""
        self.info = None
        self.state = None
        self.state_info = None
        self.is_membership = False
        self.process = None
        self.cancelled = False
        self.id = None
        self.error = None
        self.file_path = None
        self.created_at = datetime.now()


class DownloadManager:
    def __init__(self, cookie_manager):
        self.cm = cookie_manager
        self.queue = Queue()
        self.active = []
        self.sem = threading.Semaphore(config.MAX_PARALLEL)
        self._lock = threading.Lock()
        self._counter = 0
        self._callbacks = []

    def on_event(self, cb):
        self._callbacks.append(cb)

    def _emit(self, event, task, **kw):
        for cb in self._callbacks:
            try:
                cb(event, task, **kw)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def _next_id(self):
        with self._lock:
            self._counter += 1
            return self._counter

    def get_info(self, url):
        cmd = [config.YT_DLP, "--no-download", "--dump-json",
               "--no-warnings", url]
        if self.cm.cookie_valid and os.path.exists(config.COOKIE_FILE):
            cmd.extend(["--cookies", config.COOKIE_FILE])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=30)
            if result.returncode == 0 and result.stdout:
                return json.loads(result.stdout.strip().split("\n")[0])
            logger.error(f"get_info failed: {result.stderr[:300]}")
            return None
        except Exception as e:
            logger.error(f"get_info error: {e}")
            return None

    def classify(self, info):
        if not info:
            return ("error", "영상 정보를 가져올 수 없습니다", False)

        status = info.get("status") or info.get("live_status") or ""
        if status in ("private", "unavailable", "deleted", "removed"):
            return ("private", "비공개 또는 삭제된 영상입니다", False)
        if status == "upcoming":
            return ("upcoming", "아직 시작하지 않은 방송입니다", False)
        if status in ("is_live", "live", "post_live", "was_live"):
            return ("live", None, False)

        if (info.get("availability") == "member_only" or
                info.get("is_membership_video")):
            return ("membership", None, True)

        return ("normal", None, False)

    @staticmethod
    def _sanitize(name):
        return re.sub(r'[<>:"/\\|?*]', "", name).strip(". ")[:100]

    def output_template(self, info, is_membership):
        channel = (info.get("channel") or info.get("uploader") or "").lower()
        is_shachi = (
            any(n in channel for n in config.SHACHIMU_NAMES) or
            (info.get("channel_id") and
             info["channel_id"] == config.SHACHIMU_CHANNEL_ID)
        )
        ud = info.get("upload_date", "")
        yymmdd = ud[2:] if len(ud) >= 8 else datetime.now().strftime("%y%m%d")

        if is_shachi:
            fname = f"shachimu_{yymmdd}_%(title)s"
            if is_membership:
                fname += "[MEMBERSHIP]"
            fname += ".%(ext)s"
        else:
            if is_membership:
                fname = "%(title)s[MEMBERSHIP][%(id)s].%(ext)s"
            else:
                fname = "%(title)s[%(id)s].%(ext)s"
        return os.path.join(config.DOWNLOAD_DIR, fname)

    def _in_archive(self, url, vid):
        if not os.path.exists(config.ARCHIVE_FILE):
            return False
        try:
            with open(config.ARCHIVE_FILE) as f:
                text = f.read()
            return url in text or vid in text
        except Exception:
            return False

    def _add_archive(self, url, vid):
        try:
            with open(config.ARCHIVE_FILE, "a") as f:
                f.write(f"{vid}\t{url}\n")
        except Exception as e:
            logger.error(f"archive write error: {e}")

    def enqueue(self, url, requested_by):
        task = DownloadTask(url, requested_by)
        task.id = self._next_id()
        self.queue.put(task)
        logger.info(f"Task #{task.id} queued: {url}")
        self._emit("queued", task)
        self._kick()
        return task

    def _kick(self):
        if self.queue.empty():
            return
        if self.sem.acquire(blocking=False):
            task = self.queue.get()
            t = threading.Thread(target=self._worker, args=(task,), daemon=True)
            t.start()

    def _worker(self, task):
        with self._lock:
            self.active.append(task)
        try:
            self._download(task)
        finally:
            with self._lock:
                self.active.remove(task)
            self.sem.release()
            self._kick()

    def _download(self, task):
        task.status = TaskStatus.GETTING_INFO
        self._emit("info_start", task)

        info = self.get_info(task.url)
        if not info:
            # Retry with android client fallback for n-challenge
            logger.info("Retrying info fetch with android client...")
            cmd = [config.YT_DLP, "--no-download", "--dump-json",
                   "--no-warnings",
                   "--extractor-args", "youtube:player_client=android",
                   task.url]
            if os.path.exists(config.COOKIE_FILE):
                cmd.extend(["--cookies", config.COOKIE_FILE])
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0 and result.stdout:
                    info = json.loads(result.stdout.strip().split("\n")[0])
            except Exception:
                pass

        if not info:
            task.status = TaskStatus.FAILED
            task.error = "영상 정보를 가져올 수 없습니다"
            self._emit("failed", task)
            return

        task.info = info
        state, state_info, is_mem = self.classify(info)
        task.state = state
        task.state_info = state_info
        task.is_membership = is_mem

        if state in ("error", "private", "upcoming"):
            task.status = TaskStatus.FAILED
            task.error = state_info
            self._emit("failed", task)
            return

        if self._in_archive(task.url, info.get("id", "")):
            task.status = TaskStatus.FAILED
            task.error = "이미 다운로드한 영상입니다"
            self._emit("failed", task)
            return

        vid = info.get("id", "")
        template = self.output_template(info, is_mem)
        task.status = TaskStatus.DOWNLOADING
        self._emit("start", task)

        for attempt in range(config.RETRY_LIMIT):
            if task.cancelled:
                task.status = TaskStatus.CANCELLED
                self._emit("cancelled", task)
                return

            qual = config.QUALITY_PREFERENCES[
                min(attempt, len(config.QUALITY_PREFERENCES) - 1)]
            cmd = [
                config.YT_DLP,
                "-o", template,
                "--no-overwrites",
                "--merge-output-format", "mp4",
                "--ffmpeg-location", config.FFMPEG,
                "--progress", "--newline", "--no-warnings",
                "--retries", "10",
                "--extractor-retries", "3",
                "--compat-options", "no-live-chat",
                "-f", qual,
            ]
            if os.path.exists(config.COOKIE_FILE):
                cmd.extend(["--cookies", config.COOKIE_FILE])
            if state == "live":
                cmd.append("--live-from-start")
            cmd.append(task.url)

            try:
                flags = 0
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    flags = subprocess.CREATE_NO_WINDOW
                task.process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, creationflags=flags)

                last_notify = time.time()
                for line in iter(task.process.stdout.readline, ""):
                    if task.cancelled:
                        task.process.terminate()
                        task.status = TaskStatus.CANCELLED
                        self._emit("cancelled", task)
                        return
                    m = re.search(r"(\d+\.?\d*)%", line)
                    if m:
                        task.progress = float(m.group(1))
                    m = re.search(r"at\s+([\d.]+[KMGTP]?i?B/s)", line)
                    if m:
                        task.speed = m.group(1)
                    m = re.search(r"ETA\s+(\S+)", line)
                    if m:
                        task.eta = m.group(1)

                    now = time.time()
                    if now - last_notify >= config.PROGRESS_INTERVAL:
                        self._emit("progress", task)
                        last_notify = now

                task.process.wait()
                rc = task.process.returncode
                if rc == 0:
                    task.status = TaskStatus.COMPLETED
                    self._add_archive(task.url, vid)
                    self._emit("completed", task)
                    self._post_download(task, info, is_mem)
                    return
                else:
                    if attempt < config.RETRY_LIMIT - 1:
                        backoff = config.RETRY_BACKOFF[
                            min(attempt, len(config.RETRY_BACKOFF) - 1)]
                        reason = (f"다운로드 실패 (rc={rc}), "
                                  f"품질 {qual} → 다음 단계, "
                                  f"{backoff}초 후 재시도")
                        logger.warning(reason)
                        if attempt > 0:
                            self._emit("warning", task, message=reason)
                        time.sleep(backoff)
                    else:
                        task.status = TaskStatus.FAILED
                        task.error = f"모든 품질 단계 실패 (rc={rc})"
                        self._emit("failed", task)
            except Exception as e:
                logger.error(f"Download exception: {e}")
                task.status = TaskStatus.FAILED
                task.error = str(e)
                self._emit("failed", task)
                return

    def _post_download(self, task, info, is_membership):
        fname = self._get_output_filename(task, info, is_membership)
        if fname:
            task.file_path = fname

        self._record_history(task, info, is_membership, fname)

        channel = (info.get("channel") or info.get("uploader") or "").lower()
        for key, rule in config.UPLOAD_RULES.items():
            if key.lower() in channel:
                if fname and os.path.exists(fname):
                    self._upload(fname, rule["drive_path"])
                    if not rule.get("keep_local", True):
                        try:
                            os.remove(fname)
                            logger.info(f"Deleted local: {fname}")
                        except Exception as e:
                            logger.error(f"Delete failed: {e}")
                break

    @staticmethod
    def _format_size(size):
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def _record_history(self, task, info, is_membership, fpath):
        history = []
        if os.path.exists(config.HISTORY_FILE):
            try:
                with open(config.HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []

        file_size = os.path.getsize(fpath) if fpath and os.path.exists(fpath) else 0
        channel = info.get("channel") or info.get("uploader") or "Unknown"

        upload_to = ""
        for key, rule in config.UPLOAD_RULES.items():
            if key.lower() in channel.lower():
                upload_to = rule["drive_path"]
                break

        kept_local = True
        for key, rule in config.UPLOAD_RULES.items():
            if key.lower() in channel.lower() and not rule.get("keep_local", True):
                kept_local = False
                break

        entry = {
            "id": info.get("id", ""),
            "url": task.url,
            "title": info.get("title", ""),
            "channel": channel,
            "channel_id": info.get("channel_id", ""),
            "upload_date": info.get("upload_date", ""),
            "duration": info.get("duration", 0),
            "thumbnail": f"https://img.youtube.com/vi/{info.get('id','')}/mqdefault.jpg",
            "filename": os.path.basename(fpath) if fpath else "",
            "file_size": file_size,
            "file_size_str": self._format_size(file_size),
            "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_membership": is_membership,
            "upload_to": upload_to,
            "kept_local": kept_local,
            "state": task.state,
        }

        history.insert(0, entry)
        try:
            with open(config.HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"History write error: {e}")

    def _get_output_filename(self, task, info, is_membership):
        template = self.output_template(info, is_membership)
        try:
            cmd = [config.YT_DLP, "--print", "filename",
                   "-o", template, task.url]
            if os.path.exists(config.COOKIE_FILE):
                cmd.extend(["--cookies", config.COOKIE_FILE])
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception as e:
            logger.debug(f"Filename detection failed: {e}")
        return None

    def _upload(self, fpath, dest):
        try:
            logger.info(f"Uploading {fpath} → {dest}")
            result = subprocess.run(
                [config.RCLONE, "copy", fpath, dest, "--verbose"],
                capture_output=True, text=True, timeout=7200)
            if result.returncode == 0:
                logger.info(f"Upload complete: {fpath}")
            else:
                logger.error(f"Upload failed: {result.stderr[:300]}")
        except Exception as e:
            logger.error(f"Upload error: {e}")

    def cancel(self, task_id):
        with self._lock:
            for t in self.active:
                if t.id == task_id and t.status == TaskStatus.DOWNLOADING:
                    t.cancelled = True
                    return True
        new_queue = Queue()
        found = False
        while not self.queue.empty():
            t = self.queue.get()
            if t.id == task_id:
                found = True
            else:
                new_queue.put(t)
        self.queue = new_queue
        return found or False

    def status(self):
        with self._lock:
            return {
                "active": [
                    {
                        "id": t.id,
                        "url": t.url,
                        "progress": t.progress,
                        "speed": t.speed,
                        "eta": t.eta,
                        "status": t.status.value,
                    }
                    for t in self.active
                ],
                "queued": self.queue.qsize(),
            }
