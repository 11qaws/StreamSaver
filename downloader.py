import subprocess
import threading
import shutil
import os
import re
import json
import time
import logging
from queue import Queue
from enum import Enum
from datetime import datetime

import config

MAX_QUEUE_SIZE = 20    # 동시 대기열 최대 크기 (active + queued)
MIN_FREE_GB    = 2.0   # 다운로드 시작 전 최소 여유 디스크 공간

logger = logging.getLogger("StreamSaver.Downloader")
_NW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)  # CMD 창 숨김

_STALL_LIVE   = 300   # 라이브: 5분 출력 없으면 자동 취소
_STALL_NORMAL = 900   # 일반: 15분 출력 없으면 자동 취소


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
        self.downloaded = ""   # 라이브 스트림용 누적 수신량 (예: "145.2MiB")
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
        self._archive_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._counter = 0
        self._callbacks = []
        self._active_urls: set = set()   # 대기+다운로드 중인 URL 집합 (중복 방지)

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

    def _cookie_args(self):
        if (self.cm and self.cm.cookie_valid
                and os.path.exists(config.COOKIE_FILE)):
            return ["--cookies", config.COOKIE_FILE]
        return []

    def _js_runtime_args(self):
        """Node.js 경로가 있으면 --js-runtimes 반환 (yt-dlp EJS n challenge 해결)"""
        if config.NODE_JS:
            return ["--js-runtimes", f"node:{config.NODE_JS}"]
        return []

    def _run_ytdlp_info(self, url, extra_args):
        # -J (--dump-single-json): 포맷 선택 없이 전체 info 덤프 → format unavailable 오류 없음
        # --js-runtimes: Node.js로 YouTube n challenge 해결 (없으면 이미지만 반환)
        cmd = [config.YT_DLP, "-J", "--no-warnings", "--no-config",
               *self._js_runtime_args(), *extra_args, url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                               creationflags=_NW)
            if r.returncode == 0 and r.stdout:
                return json.loads(r.stdout.strip().split("\n")[0]), r.stderr
            return None, r.stderr
        except Exception as e:
            return None, str(e)

    def get_info(self, url):
        """반환: (info | None, use_cookies: bool, last_err: str)"""
        ck = self._cookie_args()

        # 1) 쿠키 없이 — yt-dlp 클라이언트 자동 선택 (공개 영상)
        # 2) 쿠키 + web 클라이언트 — 멤버십 메타데이터 추출
        #    (PO Token은 실제 스트리밍 시에만 필요, info 추출엔 불필요)
        attempts = [([], False), (ck, True)]

        err = ""
        for args, used_cookies in attempts:
            if used_cookies and not ck:
                continue
            info, err = self._run_ytdlp_info(url, args)
            if info:
                logger.info("get_info OK (cookies=%s)", used_cookies)
                return info, used_cookies, ""
            logger.warning("get_info failed (cookies=%s): %s", used_cookies, err[:200])

        err_low = err[:300].lower()
        if "sign in" in err_low or "login required" in err_low or "this video is only" in err_low:
            self._emit("warning", None,
                       message="🔑 YouTube 쿠키가 만료되었습니다. `!로그인`을 실행해 주세요.")
        logger.error("get_info all failed: %s", err[:300])
        return None, False, err[:200]

    def classify(self, info):
        if not info:
            return ("error", "영상 정보를 가져올 수 없습니다", False)

        live_status = info.get("live_status") or info.get("status") or ""
        availability = info.get("availability") or ""

        if live_status in ("private", "unavailable"):
            return ("private", "비공개 또는 삭제된 영상입니다", False)
        if live_status == "is_upcoming":
            return ("upcoming", "아직 시작하지 않은 방송입니다", False)

        is_mem = (availability == "member_only" or bool(info.get("is_membership_video")))

        # is_live / post_live: 방송 중 또는 처리 중 → --live-from-start 필요
        # was_live / None / 그 외: 이미 처리된 VOD → 일반 다운로드
        if live_status in ("is_live", "live", "post_live"):
            return ("live", None, is_mem)

        return ("normal", None, is_mem)

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
            with self._archive_lock:
                with open(config.ARCHIVE_FILE, encoding="utf-8") as f:
                    text = f.read()
            return url in text or vid in text
        except Exception:
            return False

    def _add_archive(self, url, vid):
        try:
            with self._archive_lock:
                with open(config.ARCHIVE_FILE, "a", encoding="utf-8") as f:
                    f.write(f"{vid}\t{url}\n")
        except Exception as e:
            logger.error("archive write error: %s", e)

    def enqueue(self, url, requested_by):
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("올바르지 않은 URL 스킴입니다 (http/https만 허용)")
        if len(url) > 2048:
            raise ValueError("URL이 너무 깁니다")

        with self._lock:
            total = self.queue.qsize() + len(self.active)
            if total >= MAX_QUEUE_SIZE:
                raise ValueError(f"대기열이 가득 찼습니다 (최대 {MAX_QUEUE_SIZE}개)")
            if url in self._active_urls:
                raise ValueError("이미 대기 중이거나 다운로드 중인 URL입니다")
            self._counter += 1
            task_id = self._counter
            self._active_urls.add(url)

        task = DownloadTask(url, requested_by)
        task.id = task_id
        self.queue.put(task)
        logger.info("Task #%d queued: %s", task.id, url)
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
                self._active_urls.discard(task.url)
            self.sem.release()
            self._kick()

    def _build_dl_cmd(self, task, template, qual, use_cookies, state):
        ck = self._cookie_args() if use_cookies else []
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
            *self._js_runtime_args(),
            *ck,
        ]
        if state == "live":
            cmd.append("--live-from-start")
        cmd += ["--", task.url]   # -- 이후 URL이 옵션으로 파싱되지 않도록 방지
        return cmd

    def _run_dl(self, task, cmd):
        """Popen으로 다운로드 실행. (returncode, 실제출력파일경로|None) 반환."""
        task.process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, creationflags=_NW)

        last_notify    = time.time()
        captured_file  = None   # yt-dlp stdout에서 파싱한 실제 출력 파일
        last_line      = [time.time()]   # watchdog 공유 — mutable container
        stall_limit    = _STALL_LIVE if task.state == "live" else _STALL_NORMAL

        def _watchdog():
            while task.process.poll() is None and not task.cancelled:
                time.sleep(30)
                if task.process.poll() is not None or task.cancelled:
                    break
                stall = time.time() - last_line[0]
                if stall > stall_limit:
                    logger.warning("Task #%d stalled %.0fs — terminating", task.id, stall)
                    task.process.terminate()
                    task.error = (f"{'라이브' if task.state == 'live' else '다운로드'} "
                                  f"응답 없음 {int(stall // 60)}분 초과")
                    self._emit("warning", task,
                               message=(f"⏱️ #{task.id} 오랫동안 응답이 없어 자동 취소됐습니다. "
                                        f"`/dl`로 재시도하세요."))
                    break

        threading.Thread(target=_watchdog, daemon=True,
                         name=f"watchdog-{task.id}").start()

        for line in iter(task.process.stdout.readline, ""):
            last_line[0] = time.time()
            if task.cancelled:
                task.process.terminate()
                return -1, None

            m = re.search(r"(\d+\.?\d*)%", line)
            if m:
                task.progress = float(m.group(1))
            m = re.search(r"at\s+([\d.]+[KMGTP]?i?B/s)", line)
            if m:
                task.speed = m.group(1)
            m = re.search(r"ETA\s+(\S+)", line)
            if m:
                task.eta = m.group(1)
            m = re.search(r"\[download\]\s+([\d.]+\s*[KMGTPkmg]i?B)\s+at\b", line)
            if m:
                task.downloaded = m.group(1).strip()

            # 실제 출력 파일명 파싱 — Merger 라인이 최종 파일이므로 우선
            m = re.search(r'\[Merger\] Merging formats into "(.+?)"', line)
            if m:
                captured_file = m.group(1).strip()
            elif captured_file is None:
                m = re.search(r'\[download\] Destination: (.+)', line)
                if m:
                    captured_file = m.group(1).strip()

            if time.time() - last_notify >= config.PROGRESS_INTERVAL:
                self._emit("progress", task)
                last_notify = time.time()

        task.process.wait()
        return task.process.returncode, captured_file

    def _download(self, task):
        task.status = TaskStatus.GETTING_INFO
        self._emit("info_start", task)

        # 디스크 여유 공간 확인
        try:
            check_path = config.DOWNLOAD_DIR if os.path.exists(config.DOWNLOAD_DIR) else config.BASE_DIR
            free_gb = shutil.disk_usage(check_path).free / (1024 ** 3)
            if free_gb < MIN_FREE_GB:
                task.status = TaskStatus.FAILED
                task.error = f"디스크 여유 공간 부족 ({free_gb:.1f}GB / 최소 {MIN_FREE_GB:.0f}GB 필요)"
                self._emit("failed", task)
                return
        except Exception as e:
            logger.warning("Disk space check failed: %s", e)

        info, used_cookies, info_err = self.get_info(task.url)

        if not info:
            task.status = TaskStatus.FAILED
            task.error = f"영상 정보 조회 실패: {info_err}" if info_err else "영상 정보를 가져올 수 없습니다"
            self._emit("failed", task)
            return

        task.info = info
        state, state_info, is_mem = self.classify(info)
        task.state      = state
        task.state_info = state_info
        task.is_membership = is_mem

        if state in ("error", "private", "upcoming"):
            task.status = TaskStatus.FAILED
            task.error  = state_info
            self._emit("failed", task)
            return

        if self._in_archive(task.url, info.get("id", "")):
            task.status = TaskStatus.FAILED
            task.error  = "이미 다운로드한 영상입니다"
            self._emit("failed", task)
            return

        vid      = info.get("id", "")
        template = self.output_template(info, is_mem)
        task.status = TaskStatus.DOWNLOADING
        self._emit("start", task)

        # get_info에서 결정한 쿠키 전략 그대로 품질만 낮춰가며 재시도
        retry_plan = [(q, used_cookies) for q in config.QUALITY_PREFERENCES]

        for attempt, (qual, ck) in enumerate(retry_plan):
            if task.cancelled:
                task.status = TaskStatus.CANCELLED
                self._emit("cancelled", task)
                return

            cmd = self._build_dl_cmd(task, template, qual, ck, state)
            logger.info("DL attempt %d: qual=%s cookies=%s", attempt + 1, qual, ck)

            try:
                rc, captured_file = self._run_dl(task, cmd)
            except Exception as e:
                logger.error("Download exception: %s", e)
                task.status = TaskStatus.FAILED
                task.error  = str(e)
                self._emit("failed", task)
                return

            if task.cancelled:
                task.status = TaskStatus.CANCELLED
                self._emit("cancelled", task)
                return

            if rc == 0:
                task.status = TaskStatus.COMPLETED
                self._add_archive(task.url, vid)
                self._emit("completed", task)
                self._post_download(task, info, is_mem, used_cookies, captured_file)
                return

            # 마지막 시도가 아니면 잠시 대기 후 다음 시도
            if attempt < len(retry_plan) - 1:
                backoff = config.RETRY_BACKOFF[
                    min(attempt // 2, len(config.RETRY_BACKOFF) - 1)]
                reason = (f"다운로드 실패 (rc={rc}), "
                          f"qual={qual} cookies={ck} → 재시도 {backoff}초 후")
                logger.warning(reason)
                if attempt >= 2:
                    self._emit("warning", task, message=reason)
                time.sleep(backoff)

        task.status = TaskStatus.FAILED
        task.error  = f"모든 시도 실패"
        self._emit("failed", task)

    def _cleanup_fragments(self, vid: str):
        """라이브 다운로드 후 남은 .part-Frag*.part 임시 파일 삭제"""
        if not vid:
            return
        try:
            for fname in os.listdir(config.DOWNLOAD_DIR):
                if vid in fname and ".part-Frag" in fname:
                    try:
                        os.remove(os.path.join(config.DOWNLOAD_DIR, fname))
                        logger.info("Removed fragment: %s", fname)
                    except Exception as e:
                        logger.debug("Fragment remove failed: %s", e)
        except Exception as e:
            logger.debug("Fragment cleanup error: %s", e)

    def _post_download(self, task, info, is_membership, use_cookies, captured_file=None):
        # stdout에서 파싱한 실제 파일명 우선, 없으면 yt-dlp 재호출로 예측
        if captured_file and os.path.exists(captured_file):
            fname = captured_file
            logger.debug("file path from stdout: %s", fname)
        else:
            fname = self._get_output_filename(task, info, is_membership, use_cookies)
        if fname:
            task.file_path = fname

        if task.state == "live":
            self._cleanup_fragments(info.get("id", ""))

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
            "file_path": fpath or "",
            "file_size": file_size,
            "file_size_str": self._format_size(file_size),
            "downloaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_membership": is_membership,
            "upload_to": upload_to,
            "kept_local": kept_local,
            "state": task.state,
        }

        with self._history_lock:
            history = []
            if os.path.exists(config.HISTORY_FILE):
                try:
                    with open(config.HISTORY_FILE, "r", encoding="utf-8") as f:
                        history = json.load(f)
                except Exception:
                    history = []
            history.insert(0, entry)
            tmp = config.HISTORY_FILE + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(history, f, indent=2, ensure_ascii=False)
                os.replace(tmp, config.HISTORY_FILE)
            except Exception as e:
                logger.error("History write error: %s", e)
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    def _get_output_filename(self, task, info, is_membership, use_cookies):
        template = self.output_template(info, is_membership)
        ck = self._cookie_args() if use_cookies else []
        try:
            cmd = [config.YT_DLP, "--print", "filename", "-o", template, *ck, task.url]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                               creationflags=_NW)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception as e:
            logger.debug("Filename detection failed: %s", e)
        return None

    def _upload(self, fpath, dest):
        try:
            logger.info(f"Uploading {fpath} → {dest}")
            result = subprocess.run(
                [config.RCLONE, "copy", fpath, dest, "--verbose"],
                capture_output=True, text=True, timeout=7200, creationflags=_NW)
            if result.returncode == 0:
                logger.info(f"Upload complete: {fpath}")
            else:
                logger.error(f"Upload failed: {result.stderr[:300]}")
        except Exception as e:
            logger.error(f"Upload error: {e}")

    def cancel(self, task_id):
        with self._lock:
            for t in self.active:
                if t.id == task_id:
                    t.cancelled = True
                    return True
        new_queue = Queue()
        found = False
        while not self.queue.empty():
            t = self.queue.get()
            if t.id == task_id:
                found = True
                with self._lock:
                    self._active_urls.discard(t.url)
            else:
                new_queue.put(t)
        self.queue = new_queue
        return found or False

    def shutdown(self):
        """앱 종료 시 모든 활성 다운로드 프로세스 강제 종료."""
        with self._lock:
            tasks = list(self.active)
        for task in tasks:
            task.cancelled = True
            if task.process and task.process.poll() is None:
                try:
                    task.process.kill()
                    logger.info("Killed download process for task #%d", task.id)
                except Exception as e:
                    logger.debug("Process kill error: %s", e)
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except Exception:
                pass
        with self._lock:
            self._active_urls.clear()

    def status(self):
        with self._lock:
            queue_items = list(self.queue.queue)   # deque snapshot under lock
            return {
                "active": [
                    {
                        "id":           t.id,
                        "url":          t.url,
                        "title":        t.info.get("title", "") if t.info else "",
                        "requested_by": t.requested_by or "",
                        "progress":     t.progress,
                        "speed":        t.speed,
                        "eta":          t.eta,
                        "downloaded":   t.downloaded,
                        "state":        t.state,
                        "status":       t.status.value,
                    }
                    for t in self.active
                ],
                "queued":     self.queue.qsize(),
                "queue_list": [{"id": t.id, "url": t.url, "requested_by": t.requested_by or ""} for t in queue_items],
            }
