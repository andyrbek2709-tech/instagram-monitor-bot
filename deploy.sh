#!/bin/bash

set -e

echo "Instagram Monitor Bot Deployment Script"
echo "========================================"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[1] Creating data and logs directories..."
mkdir -p data/backups
mkdir -p logs

echo "[2] Checking .env file..."
if [ ! -f ".env" ]; then
    echo "ERROR: .env file not found!"
    echo "Please copy .env.template to .env and fill in your API keys."
    exit 1
fi

echo "[3] Validating Python installation..."
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python3 is not installed!"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python version: $PYTHON_VERSION"

echo "[4] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "[5] Initializing database..."
python3 db_init.py

echo "[6] Running environment validation..."
python3 -c "from main import validate_environment; validate_environment(); print('✓ Environment validation passed')"

echo ""
echo "========================================"
echo "Deployment Options:"
echo "========================================"
echo ""
echo "Option 1: Docker Deployment (Recommended)"
echo "  docker-compose up -d"
echo ""
echo "Option 2: Systemd Service (Linux VPS)"
echo "  sudo cp instagram-monitor-bot.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable instagram-monitor-bot"
echo "  sudo systemctl start instagram-monitor-bot"
echo ""
echo "Option 3: Direct Execution"
echo "  python3 main.py"
echo ""
echo "========================================"
echo "Deployment Complete!"
echo "========================================"
