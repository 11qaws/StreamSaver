# StreamSaver

Discord 봇 — YouTube 멤버십 영상을 자동으로 다운로드합니다.
디스코드에 URL을 보내면 멤버십/공개/라이브/녹화 영상을 자동으로 받고,
Google Drive 업로드 + 웹 대시보드로 내역을 확인할 수 있습니다.

## 주요 기능

- **`!dl <URL>`** — 유튜브 영상 다운로드 (멤버십/공개/라이브/VOD 모두 지원)
- **자동 쿠키 관리** — Edge 브라우저 백그라운드 실행, 30분마다 자동 쿠키 갱신, 사용자 조작 불필요
- **품질 폴백** — 최고 화질 실패 시 자동으로 단계적 다운그레이드 (1080p → 720p → 최저)
- **병렬 다운로드** — 동시 최대 5개, 초과 시 자동 대기열
- **Google Drive 업로드** — 채널별 업로드 규칙 (예: Shachimu 영상은 GDrive 전용 보관)
- **웹 대시보드** — `http://localhost:8080` 에서 다운로드 내역 검색/필터링/정렬
- **시스템 트레이** — 백그라운드 실행, Windows 토스트 알림
- **절전 방지** — 다운로드 중 PC가 절전모드로 안 들어감
- **GitHub 자동 동기화** — 다운로드 완료 시 history.json이 자동으로 GitHub에 푸시됨

## 명령어

| 명령어 | 설명 |
|---------|------|
| `!dl <URL>` | 영상 다운로드 대기열 추가 |
| `!취소 <번호>` | 진행 중인 작업 취소 |
| `!대기열` | 현재 대기열 상태 확인 |
| `!상태` | 봇 상태 확인 (쿠키/대기열) |
| `!로그인` | Edge 브라우저 열어서 YouTube 로그인 (최초 1회) |
| `!로그` | 최근 로그 출력 |

## 웹 대시보드

봇 실행 후 `http://localhost:8080` 접속:
- 다운로드한 전체 파일 목록 (카드 리스트 + 썸네일)
- 제목/채널 검색, 채널별 필터, 멤버십 전용 필터
- 정렬: 최신순 / 오래된순 / 큰순 / 작은순
- 자동 30초 갱신

GitHub Pages 에서도 동일한 대시보드 사용 가능:
`https://11qaws.github.io/StreamSaver/`

## 설치

### 요구사항
- Windows 10/11
- Python 3.10+
- yt-dlp (`E:\FFMPEG\bin\yt-dlp.exe`)
- ffmpeg (`E:\FFMPEG\bin\ffmpeg.exe`)
- rclone (선택, GDrive 업로드 시)
- Edge 브라우저

### 설치 방법

```cmd
git clone https://github.com/11qaws/StreamSaver.git
cd StreamSaver

python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

### 설정

1. `.env.example` → `.env` 로 복사하고 Discord 봇 토큰 입력:
   ```
   DISCORD_TOKEN=your_token_here
   ```

2. 레지스트리 키 등록 (Edge 쿠키 추출, 1회):
   ```
   HKLM\SOFTWARE\Policies\Microsoft\Edge
   AdditionalLaunchParameters = --disable-features=msAppBoundEncryption
   ```

3. 봇 실행:
   ```cmd
   run_bot.bat
   ```

4. Discord에서 `!로그인` 명령어 실행
5. Edge 브라우저가 열리면 YouTube 로그인 후 종료
6. 자동으로 쿠키 추출 및 백그라운드 실행

## 설정

`config.py` 에서 변경:

| 항목 | 기본값 | 설명 |
|------|--------|------|
| `MAX_PARALLEL` | 5 | 동시 다운로드 개수 |
| `PROGRESS_INTERVAL` | 7 | 진행률 Discord 전송 간격 (초) |
| `WEB_PORT` | 8080 | 웹 대시보드 포트 |
| `UPLOAD_RULES` | Shachi → GDrive | 채널별 업로드 규칙 |
| `QUALITY_PREFERENCES` | best → 1080p → 720p | 품질 폴백 체인 |
| `DISCORD_CHANNEL_ID` | 1520389415564742726 | 명령어 허용 채널 |

### 업로드 규칙 예시

```python
UPLOAD_RULES = {
    "shachi too": {
        "drive_path": "gdrive:Hololive/Mumei/Shachimu/",
        "keep_local": False,     # GDrive 업로드 후 로컬 삭제
    },
    # 필요에 따라 추가:
    # "other channel": {
    #     "drive_path": "gdrive:Some/Path/",
    #     "keep_local": True,    # 로컬에도 보관
    # },
}
```

## 라이선스

MIT
