import os
import sys
import platform
import shutil
from dotenv import load_dotenv

# ── frozen(PyInstaller) 여부 감지 ─────────────────────────────────────────────
_FROZEN = getattr(sys, 'frozen', False)

if _FROZEN:
    _INSTALL_DIR = os.path.dirname(sys.executable)           # exe + yt-dlp + ffmpeg
    _DATA_DIR    = getattr(sys, '_MEIPASS', _INSTALL_DIR)   # index.html 등 번들 데이터 (_internal)
    BASE_DIR     = os.path.join(os.environ.get('LOCALAPPDATA', _INSTALL_DIR), 'StreamSaver')
    os.makedirs(BASE_DIR, exist_ok=True)
else:
    _INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
    _DATA_DIR    = _INSTALL_DIR
    BASE_DIR     = _INSTALL_DIR

load_dotenv(os.path.join(BASE_DIR, ".env"), override=True, encoding='utf-8-sig')

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
_ch = os.getenv("DISCORD_CHANNEL_ID", "")
DISCORD_CHANNEL_ID = int(_ch) if _ch.isdigit() else None

# ── 릴레이 서버 설정 ─────────────────────────────────────────────────────────
RELAY_SERVER_URL = os.getenv("RELAY_SERVER_URL", "")
RELAY_SECRET     = os.getenv("RELAY_SECRET", "")
RELAY_PAIR_CODE  = os.getenv("RELAY_PAIR_CODE", "")

DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
LOG_DIR      = os.path.join(BASE_DIR, "logs")
ARCHIVE_FILE = os.path.join(BASE_DIR, "archive.txt")
COOKIE_FILE  = os.path.join(BASE_DIR, "cookie.txt")
HISTORY_FILE = os.path.join(DOWNLOAD_DIR, "history.json")
INDEX_HTML   = os.path.join(_DATA_DIR, "index.html")   # PyInstaller _internal 안에 있음
WEB_PORT     = 8080

if platform.system() == "Windows":
    def _bin(name, fallback):
        local = os.path.join(_INSTALL_DIR, name)
        return local if os.path.exists(local) else fallback

    YT_DLP = _bin("yt-dlp.exe",  r"E:\FFMPEG\bin\yt-dlp.exe")
    RCLONE = _bin("rclone.exe",  r"E:\FFMPEG\bin\rclone.exe")
    FFMPEG = _bin("ffmpeg.exe",  r"E:\FFMPEG\bin\ffmpeg.exe")

    def _find_edge():
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            os.path.join(os.environ.get('LOCALAPPDATA', ''),
                         r"Microsoft\Edge\Application\msedge.exe"),
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
        return ""   # 미발견 — cookie 기능 비활성화로 처리

    EDGE_PATH        = _find_edge()
    BOT_EDGE_PROFILE = (os.path.join(BASE_DIR, "edge_profile")
                        if _FROZEN else r"E:\FFMPEG\edge_bot_profile")
else:
    YT_DLP           = "/home/qumin/.local/bin/yt-dlp"
    RCLONE           = "rclone"
    FFMPEG           = "/home/qumin/bin/ffmpeg"
    EDGE_PATH        = ""
    BOT_EDGE_PROFILE = ""

MAX_PARALLEL            = 5
PROGRESS_INTERVAL       = 7
COOKIE_REFRESH_INTERVAL = 1800
RETRY_LIMIT             = 3
RETRY_BACKOFF           = [5, 15, 30]

WATCH_POLL_INTERVAL = 300

QUALITY_PREFERENCES = [
    "best",
    "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "worst"
]

UPLOAD_RULES        = {}
SHACHIMU_NAMES      = []
SHACHIMU_CHANNEL_ID = ""

try:
    from config_personal import UPLOAD_RULES, SHACHIMU_NAMES, SHACHIMU_CHANNEL_ID
except ImportError:
    pass

NODE_JS = shutil.which('node') or ''

APP_VERSION    = "1.0.7"
GITHUB_REPO    = "11qaws/StreamSaver"
BOT_INVITE_URL = (
    "https://discord.com/oauth2/authorize"
    "?client_id=1520388249040978100"
    "&permissions=84992"
    "&scope=bot+applications.commands"
)
