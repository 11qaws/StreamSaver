#!/bin/bash
# StreamSaver Relay Server — Oracle Cloud Oracle Linux 자동 세팅 스크립트
# 사용법: bash deploy.sh

set -e

echo "======================================"
echo "  StreamSaver Relay Server 설치 시작"
echo "======================================"

# ── 1. 시스템 업데이트 ─────────────────────────────────────────────────────
echo "[1/6] 시스템 업데이트..."
sudo dnf update -y -q

# ── 2. Python 설치 ─────────────────────────────────────────────────────────
echo "[2/6] Python 설치..."
if sudo dnf install -y python3.11 python3.11-pip 2>/dev/null; then
    PYTHON=python3.11
else
    sudo dnf install -y python3 python3-pip
    PYTHON=python3
fi

# ── 3. 서버 파일 설치 ──────────────────────────────────────────────────────
echo "[3/6] 서버 파일 설치..."
sudo mkdir -p /opt/streamsaver-relay
sudo cp server.py requirements.txt /opt/streamsaver-relay/
cd /opt/streamsaver-relay

sudo $PYTHON -m venv venv
sudo venv/bin/pip install -q --upgrade pip
sudo venv/bin/pip install -q -r requirements.txt

# ── 4. .env 파일 생성 ──────────────────────────────────────────────────────
echo "[4/6] 환경 변수 설정..."

if [ ! -f /opt/streamsaver-relay/.env ]; then
    echo ""
    echo "Discord 봇 토큰을 입력하세요 (Discord Developer Portal에서 복사):"
    read -r DISCORD_TOKEN

    echo ""
    echo "WebSocket 인증 비밀키를 입력하세요 (아무 문자열, 예: mysecret123):"
    read -r WS_SECRET

    sudo tee /opt/streamsaver-relay/.env > /dev/null << EOF
DISCORD_TOKEN=${DISCORD_TOKEN}
WS_SECRET=${WS_SECRET}
WS_PORT=8765
EOF
    echo ".env 파일 생성 완료"
else
    echo ".env 파일이 이미 존재합니다. 건너뜁니다."
fi

# ── 5. systemd 서비스 등록 ─────────────────────────────────────────────────
echo "[5/6] 자동 시작 서비스 등록..."

sudo tee /etc/systemd/system/streamsaver-relay.service > /dev/null << 'EOF'
[Unit]
Description=StreamSaver Relay Server
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/streamsaver-relay
EnvironmentFile=/opt/streamsaver-relay/.env
ExecStart=/opt/streamsaver-relay/venv/bin/python server.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable streamsaver-relay
sudo systemctl start streamsaver-relay

# ── 6. 방화벽 포트 개방 ────────────────────────────────────────────────────
echo "[6/6] 방화벽 포트 8765 개방..."
sudo firewall-cmd --permanent --add-port=8765/tcp
sudo firewall-cmd --reload

echo ""
echo "======================================"
echo "  설치 완료!"
echo "======================================"
echo ""
echo "서버 상태 확인: sudo systemctl status streamsaver-relay"
echo "실시간 로그:    sudo journalctl -u streamsaver-relay -f"
echo ""

PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "확인 불가")
echo "이 서버의 공인 IP: ${PUBLIC_IP}"
echo ""
echo "Windows .env에 다음을 추가하세요:"
echo "RELAY_SERVER_URL=ws://${PUBLIC_IP}:8765"
echo "RELAY_SECRET=<위에서 입력한 비밀키>"
