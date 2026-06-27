import json
import os
import logging
import http.server
import threading
import urllib.parse

import config

logger = logging.getLogger("StreamSaver.Web")

_cache = {"history": None, "mtime": 0}


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
            else:
                self.send_error(404)
        except Exception as e:
            logger.error(f"HTTP error: {e}")
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

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

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

        page = int((qs.get("page") or ["1"])[0])
        per_page = int((qs.get("per_page") or ["20"])[0])
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

    def log_message(self, fmt, *args):
        logger.debug(f"HTTP: {fmt % args}")


def start():
    server = http.server.HTTPServer(("0.0.0.0", config.WEB_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Web server started → http://localhost:{config.WEB_PORT}")
