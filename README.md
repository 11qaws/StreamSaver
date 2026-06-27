# StreamSaver

A Discord bot that automatically downloads YouTube membership streams/videos.
Receives URL commands via Discord, manages Edge browser cookies for membership access,
and supports uploads to Google Drive via rclone.

## Features

- **!dl** — Download any YouTube video (public, membership, live, VOD)
- **Web Dashboard** — Browse download history with search, filter, sort
- **Auto Cookie Management** — 30-minute Edge cookie refresh, no manual intervention
- **Quality Fallback** — Automatically tries lower resolutions if best fails
- **Parallel Downloads** — Configurable concurrent downloads (default 5)
- **Google Drive Upload** — Per-channel upload rules (e.g., Shachi → GDrive only)
- **System Tray** — Background operation with Windows notifications
- **Power Management** — Prevents sleep during downloads

## Commands

| Command | Description |
|---------|-------------|
| `!dl <URL>` | Add video to download queue |
| `!취소 <id>` | Cancel a queued or running download |
| `!대기열` | Show queue status |
| `!상태` | Show bot status (cookies, queue) |
| `!로그인` | Open Edge for YouTube login (one-time setup) |
| `!로그` | Show recent log entries |

## Web Dashboard

Start the bot and visit `http://localhost:8080` to browse your download history.
The dashboard supports search, channel filtering, and sort by date/size.

## Setup

1. Install Python 3.10+ and create a virtual environment:
   ```
   python -m venv venv
   venv\Scripts\pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and set your Discord bot token:
   ```
   DISCORD_TOKEN=your_token_here
   ```

3. Set up registry key for Edge cookie extraction (one-time):
   ```
   HKLM\SOFTWARE\Policies\Microsoft\Edge
   AdditionalLaunchParameters = --disable-features=msAppBoundEncryption
   ```

4. Run the bot:
   ```
   run_bot.bat
   ```

5. In Discord, run `!로그인` and log into YouTube when Edge opens, then close Edge.

## Configuration

Edit `config.py` to customize:

- `MAX_PARALLEL` — Max concurrent downloads (default 5)
- `UPLOAD_RULES` — Per-channel Google Drive paths
- `QUALITY_PREFERENCES` — Quality fallback chain
- `DISCORD_CHANNEL_ID` — Allowed command channel

## License

MIT
