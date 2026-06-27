import os
import platform
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = 1520389415564742726
DISCORD_SERVER_ID = 236766834563612673

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
LOG_DIR = os.path.join(BASE_DIR, "logs")
ARCHIVE_FILE = os.path.join(BASE_DIR, "archive.txt")
COOKIE_FILE = os.path.join(BASE_DIR, "cookie.txt")
HISTORY_FILE = os.path.join(DOWNLOAD_DIR, "history.json")
INDEX_HTML = os.path.join(BASE_DIR, "index.html")
WEB_PORT = 8080

if platform.system() == "Windows":
    YT_DLP = r"E:\FFMPEG\bin\yt-dlp.exe"
    RCLONE = r"E:\FFMPEG\bin\rclone.exe"
    FFMPEG = r"E:\FFMPEG\bin\ffmpeg.exe"
    EDGE_PATH = r"E:\FFMPEG\edge_portable\msedge.exe"
    BOT_EDGE_PROFILE = r"E:\FFMPEG\edge_bot_profile"
    CDP_PORT = 9222
else:
    YT_DLP = "/home/qumin/.local/bin/yt-dlp"
    RCLONE = "rclone"
    FFMPEG = "/home/qumin/bin/ffmpeg"
    EDGE_PATH = ""
    BOT_EDGE_PROFILE = ""
    CDP_PORT = 0

MAX_PARALLEL = 5
PROGRESS_INTERVAL = 7
COOKIE_REFRESH_INTERVAL = 1800
RETRY_LIMIT = 3
RETRY_BACKOFF = [5, 15, 30]

QUALITY_PREFERENCES = [
    "best",
    "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "worst"
]

UPLOAD_RULES = {
    "shachi too": {
        "drive_path": "gdrive:Hololive/Mumei/Shachimu/",
        "keep_local": False,
    },
}

SHACHIMU_NAMES = ["shachi too", "shachimu", "@shachitoo"]
SHACHIMU_CHANNEL_ID = ""
