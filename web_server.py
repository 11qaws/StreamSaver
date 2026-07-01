import json
import os
import logging
import http.server
import threading
import urllib.parse
import shutil

import config

logger = logging.getLogger("StreamSaver.Web")

_cache = {"history": None, "mtime": 0}
_cm = None
_dm = None
_sw = None
_gui = None
_rc = None   # relay client


def set_context(cm, dm):
    global _cm, _dm
    _cm = cm
    _dm = dm


def _load_history():
    path = config.HISTORY_FILE
    if not os.path.exists(path):
        return []
    mtime = os.path.getmtime(path)
    if mtime != _cache["mtime"]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                _cache["history"] = json.load(f)
            _cache["mtime"] = mtime
        except Exception:
            _cache["history"] = []
    return _cache["history"]


_MAX_BODY = 1 * 1024 * 1024  # 1 MB


def _read_body(rfile, headers):
    length = int(headers.get("Content-Length") or 0)
    if length <= 0:
        return b""
    if length > _MAX_BODY:
        raise ValueError(f"Request body too large ({length} bytes)")
    return rfile.read(length)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/":
                self._file(config.INDEX_HTML, "text/html; charset=utf-8")
            elif path == "/api/history":
                self._history(qs)
            elif path == "/api/stats":
                self._stats()
            elif path == "/api/status":
                self._live_status()
            elif path == "/api/channels":
                self._channels_list()
            elif path == "/api/version":
                self._json({"version": config.APP_VERSION, "repo": config.GITHUB_REPO})
            elif path == "/api/open-folder":
                self._open_folder(qs)
            elif path == "/api/settings":
                self._settings()
            elif path == "/api/update-status":
                self._update_status()
            elif path == "/api/export":
                self._export(qs)
            elif path == "/api/video-info":
                self._video_info(qs)
            else:
                self.send_error(404)
        except Exception as e:
            logger.error(f"HTTP GET error: {e}")
            try:
                self.send_error(500)
            except Exception:
                pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = _read_body(self.rfile, self.headers)
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        try:
            if path == "/api/cancel":
                self._cancel(data)
            elif path == "/api/channels":
                self._channels_add(data)
            elif path == "/api/download":
                self._download_add(data)
            elif path == "/api/cleanup":
                self._cleanup()
            elif path == "/api/update-install":
                self._update_install()
            else:
                self.send_error(404)
        except Exception as e:
            logger.error(f"HTTP POST error: {e}")
            try:
                self.send_error(500)
            except Exception:
                pass

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/channels":
                self._channels_remove(qs)
            elif path == "/api/history":
                self._history_delete(qs)
            else:
                self.send_error(404)
        except Exception as e:
            logger.error(f"HTTP DELETE error: {e}")
            try:
                self.send_error(500)
            except Exception:
                pass

    def _file(self, filepath, content_type):
        if not os.path.exists(filepath):
            self.send_error(404)
            return
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_error(500)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _live_status(self):
        cm = _cm
        dm = _dm
        cs = cm.get_status() if cm else {}
        ds = dm.status() if dm else {}
        try:
            du = shutil.disk_usage(config.DOWNLOAD_DIR)
            disk_free_gb = round(du.free / (1024 ** 3), 1)
            disk_total_gb = round(du.total / (1024 ** 3), 1)
        except Exception:
            disk_free_gb = 0
            disk_total_gb = 0
        rc = _rc
        self._json({
            "cookie_valid":           cs.get("cookie_valid", False),
            "cookie_file":            cs.get("cookie_file", False),
            "cookie_days_remaining":  cs.get("cookie_days_remaining"),
            "edge_running":           cs.get("edge_running", False),
            "relay_connected":        rc.connected if rc else False,
            "bot_discord":            rc.bot_discord if rc else False,
            "active":                 ds.get("active", []),
            "queued":                 ds.get("queued", 0),
            "queue_list":             ds.get("queue_list", []),
            "disk_free_gb":           disk_free_gb,
            "disk_total_gb":          disk_total_gb,
        })

    def _settings(self):
        try:
            du = shutil.disk_usage(config.DOWNLOAD_DIR)
            disk_free_gb = round(du.free / (1024 ** 3), 1)
            disk_total_gb = round(du.total / (1024 ** 3), 1)
        except Exception:
            disk_free_gb = 0
            disk_total_gb = 0
        cm = _cm
        cs = cm.get_status() if cm else {}
        self._json({
            "version":                config.APP_VERSION,
            "download_dir":           config.DOWNLOAD_DIR,
            "max_parallel":           config.MAX_PARALLEL,
            "watch_poll_interval":    config.WATCH_POLL_INTERVAL,
            "disk_free_gb":           disk_free_gb,
            "disk_total_gb":          disk_total_gb,
            "repo":                   config.GITHUB_REPO,
            "cookie_valid":           cs.get("cookie_valid", False),
            "cookie_days_remaining":  cs.get("cookie_days_remaining"),
        })

    def _video_info(self, qs):
        import re, subprocess
        url = (qs.get("url") or [""])[0].strip()
        if not url:
            self._json({"error": "no url"}, 400)
            return
        if not re.search(r'(youtube\.com/watch|youtu\.be/|youtube\.com/shorts)', url):
            self._json({"error": "not a youtube url"}, 400)
            return
        try:
            result = subprocess.run(
                [config.YT_DLP, "--dump-json", "--no-playlist", "--skip-download", "-q", url],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0 or not result.stdout.strip():
                self._json({"error": "fetch failed"}, 502)
                return
            info = json.loads(result.stdout)
            vid_id = info.get("id", "")
            in_archive = False
            if vid_id and os.path.exists(config.ARCHIVE_FILE):
                try:
                    with open(config.ARCHIVE_FILE, encoding="utf-8") as f:
                        in_archive = any(vid_id in ln for ln in f)
                except Exception:
                    pass
            self._json({
                "id":          vid_id,
                "title":       info.get("title", ""),
                "channel":     info.get("channel") or info.get("uploader", ""),
                "duration":    info.get("duration"),
                "thumbnail":   info.get("thumbnail", ""),
                "upload_date": info.get("upload_date", ""),
                "in_archive":  in_archive,
            })
        except subprocess.TimeoutExpired:
            self._json({"error": "timeout"}, 504)
        except Exception as e:
            logger.warning(f"video-info error: {e}")
            self._json({"error": str(e)}, 500)

    def _export(self, qs):
        history = _load_history()
        fmt = (qs.get("format") or ["json"])[0].lower()
        if fmt == "csv":
            import io
            buf = io.StringIO()
            cols = ["title", "channel", "id", "upload_date", "downloaded_at",
                    "duration", "file_size", "is_membership", "file_path"]
            buf.write(",".join(cols) + "\r\n")
            for e in history:
                row = [
                    '"' + str(e.get("title", "")).replace('"', '""') + '"',
                    '"' + str(e.get("channel", "")).replace('"', '""') + '"',
                    str(e.get("id", "")),
                    str(e.get("upload_date", "")),
                    str(e.get("downloaded_at", "")),
                    str(e.get("duration", "")),
                    str(e.get("file_size", "")),
                    "1" if e.get("is_membership") else "0",
                    '"' + str(e.get("file_path", "")).replace('"', '""') + '"',
                ]
                buf.write(",".join(row) + "\r\n")
            data = buf.getvalue().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=streamsaver_history.csv")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        else:
            data = json.dumps(history, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=streamsaver_history.json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

    def _download_add(self, data):
        dm = _dm
        if not dm:
            self._json({"ok": False, "error": "다운로더를 사용할 수 없습니다"}, 503)
            return
        url = (data.get("url") or "").strip()
        if not url:
            self._json({"ok": False, "error": "URL이 필요합니다"}, 400)
            return
        force = bool(data.get("force"))
        try:
            task = dm.enqueue(url, "dashboard", force=force)
            self._json({"ok": True, "id": task.id})
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            logger.error(f"download_add error: {e}")
            self._json({"ok": False, "error": "내부 오류가 발생했습니다"}, 500)

    def _cleanup(self):
        path = config.HISTORY_FILE
        if not os.path.exists(path):
            self._json({"ok": True, "removed": 0})
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        def _file_exists(e):
            if e.get("kept_local") is False:
                return True  # Drive 업로드 후 삭제된 파일 — 기록은 유지
            fp = e.get("file_path", "")
            if fp:
                return os.path.exists(fp)
            fname = e.get("filename", "")
            if fname:
                return os.path.exists(os.path.join(config.DOWNLOAD_DIR, fname))
            return True  # no path info → keep

        kept = [e for e in history if _file_exists(e)]
        removed = len(history) - len(kept)
        if removed > 0:
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(kept, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
                _cache["mtime"] = 0  # invalidate cache
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
                return
        self._json({"ok": True, "removed": removed, "kept": len(kept)})

    def _history_delete(self, qs):
        vid_id = (qs.get("id") or [""])[0].strip()
        if not vid_id:
            self._json({"ok": False, "error": "id required"}, 400)
            return
        path = config.HISTORY_FILE
        if not os.path.exists(path):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        new_history = [e for e in history if e.get("id") != vid_id]
        if len(new_history) == len(history):
            self._json({"ok": False, "error": "entry not found"}, 404)
            return
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(new_history, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            _cache["mtime"] = 0
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        self._json({"ok": True})

    def _cancel(self, data):
        dm = _dm
        if not dm:
            self._json({"ok": False, "error": "not available"}, 503)
            return
        task_id = data.get("id")
        if task_id is None:
            self._json({"ok": False, "error": "id required"}, 400)
            return
        try:
            ok = dm.cancel(int(task_id))
        except (ValueError, TypeError):
            self._json({"ok": False, "error": "id must be an integer"}, 400)
            return
        self._json({"ok": ok})

    def _channels_list(self):
        sw = _sw
        if not sw:
            self._json({"channels": []})
            return
        channels = [
            {"url": url, "name": info.get("name", ""), "handle": info.get("handle", ""),
             "title_filter": info.get("title_filter", "")}
            for url, info in sw.list_channels()
        ]
        self._json({"channels": channels})

    def _channels_add(self, data):
        sw = _sw
        if not sw:
            self._json({"ok": False, "error": "not available"}, 503)
            return
        url = (data.get("url") or "").strip()
        if not url:
            self._json({"ok": False, "error": "url required"}, 400)
            return
        name = (data.get("name") or "").strip()
        title_filter = (data.get("title_filter") or "").strip()
        display = sw.add(url, name=name, title_filter=title_filter)
        self._json({"ok": True, "display": display})

    def _channels_remove(self, qs):
        sw = _sw
        if not sw:
            self._json({"ok": False, "error": "not available"}, 503)
            return
        url = (qs.get("url") or [""])[0].strip()
        if not url:
            self._json({"ok": False, "error": "url required"}, 400)
            return
        ok = sw.remove(url)
        self._json({"ok": ok})

    def _history(self, qs):
        history = _load_history()

        search = (qs.get("search") or [""])[0].strip().lower()
        channel = (qs.get("channel") or [""])[0].strip().lower()
        membership = (qs.get("membership") or [""])[0].strip()

        filtered = history
        if search:
            filtered = [
                e for e in filtered
                if search in e.get("title", "").lower()
                or search in e.get("channel", "").lower()
            ]
        if channel:
            filtered = [
                e for e in filtered
                if channel in e.get("channel", "").lower()
            ]
        if membership == "true":
            filtered = [e for e in filtered if e.get("is_membership")]
        elif membership == "false":
            filtered = [e for e in filtered if not e.get("is_membership")]

        sort = (qs.get("sort") or ["newest"])[0]
        if sort == "oldest":
            filtered = list(reversed(filtered))
        elif sort == "largest":
            filtered = list(sorted(filtered, key=lambda x: x.get("file_size", 0), reverse=True))
        elif sort == "smallest":
            filtered = list(sorted(filtered, key=lambda x: x.get("file_size", 0)))

        page = max(1, int((qs.get("page") or ["1"])[0]))
        per_page = max(1, min(5000, int((qs.get("per_page") or ["20"])[0])))
        total = len(filtered)
        start = (page - 1) * per_page
        end = start + per_page

        self._json({
            "items": filtered[start:end],
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        })

    def _stats(self):
        history = _load_history()
        channels = {}
        total_size = 0
        membership_count = 0

        for e in history:
            ch = e.get("channel", "Unknown")
            channels[ch] = channels.get(ch, 0) + 1
            total_size += e.get("file_size", 0)
            if e.get("is_membership"):
                membership_count += 1

        s = total_size
        for unit in ["B", "KB", "MB", "GB"]:
            if s < 1024:
                size_str = f"{s:.1f} {unit}"
                break
            s /= 1024
        else:
            size_str = f"{s:.1f} TB"

        self._json({
            "total_files": len(history),
            "total_size": total_size,
            "total_size_str": size_str,
            "channel_count": len(channels),
            "channels": [
                {"name": k, "count": v}
                for k, v in sorted(channels.items(), key=lambda x: -x[1])
            ],
            "membership_count": membership_count,
        })

    def _update_status(self):
        gui = _gui
        if not gui:
            self._json({"available": False})
            return
        info = gui._update_info
        if not info:
            self._json({"available": False})
            return
        self._json({
            "available":    True,
            "version":      info.get("version", ""),
            "notes":        info.get("notes", ""),
            "downloading":  gui._update_progress is not None,
            "progress":     gui._update_progress,
            "ready":        gui._installer_path is not None,
        })

    def _update_install(self):
        gui = _gui
        if not gui:
            self._json({"ok": False, "error": "not available"}, 503)
            return
        if not gui._installer_path:
            self._json({"ok": False, "error": "installer not ready"}, 409)
            return
        import threading
        threading.Thread(target=gui._confirm_and_install, daemon=True).start()
        self._json({"ok": True})

    def _open_folder(self, qs):
        import subprocess
        file_path = (qs.get("path") or [""])[0].strip()
        if not file_path:
            self._json({"ok": False, "error": "path required"}, 400)
            return
        try:
            normed = os.path.normpath(file_path)
            if os.path.exists(normed):
                subprocess.Popen(
                    ["explorer", f"/select,{normed}"],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                parent = os.path.dirname(normed)
                target = parent if os.path.exists(parent) else config.DOWNLOAD_DIR
                subprocess.Popen(
                    ["explorer", os.path.normpath(target)],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def log_message(self, fmt, *args):
        logger.debug(f"HTTP: {fmt % args}")


def start(ctx=None):
    global _cm, _dm, _sw, _gui, _rc
    if ctx:
        _cm = ctx.cm
        _dm = ctx.dm
        _sw = getattr(ctx, "sw", None)
        _gui = getattr(ctx, "gui", None)
        _rc = getattr(ctx, "rc", None)
    server = http.server.HTTPServer(("127.0.0.1", config.WEB_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Web server → http://localhost:{config.WEB_PORT}")
