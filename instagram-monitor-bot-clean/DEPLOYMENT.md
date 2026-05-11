# Instagram Monitor Bot - Deployment Guide

## Quick Start

### Prerequisites
- Python 3.11+
- Docker & Docker Compose (for containerized deployment)
- VPS/Server with internet access (DigitalOcean, Railway, Heroku, AWS, etc.)

### Step 1: Prepare Environment
```bash
cp .env.template .env
# Edit .env with your actual API keys and credentials
```

### Step 2: Run Deployment Script
```bash
chmod +x deploy.sh
./deploy.sh
```

---

## Deployment Options

### Option 1: Docker (Recommended for Cloud)

**Local Testing:**
```bash
docker-compose up
```

**Production Deployment:**
```bash
docker-compose up -d
```

Check status:
```bash
docker-compose ps
docker-compose logs -f
```

### Option 2: Systemd Service (Linux VPS)

**Prerequisites:**
- Ubuntu/Debian-based VPS
- SSH access with sudo

**Installation:**
```bash
# Create bot user
sudo useradd -m -s /bin/bash bot

# Copy project to VPS
scp -r . bot@your_vps:/home/bot/instagram-monitor-bot

# SSH into VPS
ssh bot@your_vps

# Install dependencies
cd instagram-monitor-bot
pip3 install -r requirements.txt

# Setup logs directory
sudo mkdir -p /var/log/instagram-monitor-bot
sudo chown bot:bot /var/log/instagram-monitor-bot

# Copy service file
sudo cp instagram-monitor-bot.service /etc/systemd/system/

# Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable instagram-monitor-bot
sudo systemctl start instagram-monitor-bot
```

**Management:**
```bash
# Check status
sudo systemctl status instagram-monitor-bot

# View logs
sudo journalctl -u instagram-monitor-bot -f

# Restart
sudo systemctl restart instagram-monitor-bot

# Stop
sudo systemctl stop instagram-monitor-bot
```

### Option 3: Direct Execution

For development or simple deployments:
```bash
python3 main.py
```

---

## Recommended Cloud Providers

### Railway (Simple, Recommended)
1. Push code to GitHub
2. Connect GitHub to Railway
3. Set environment variables in Railway dashboard
4. Deploy with one click

### DigitalOcean Droplet
1. Create Ubuntu 22.04 Droplet
2. SSH and run deployment script
3. Use systemd service for 24/7 operation

### Heroku (Legacy)
1. Create Procfile:
```
worker: python main.py
```
2. Deploy via git push

---

## Relay/Proxy Configuration (For Instagram Scraping)

If Instagram blocks requests:

### Option 1: Bright Data (Residential Proxy)
```python
# In parser.py, modify requests session:
proxies = {
    'http': 'http://username:password@proxy.bright.com:port',
    'https': 'http://username:password@proxy.bright.com:port'
}
session.proxies.update(proxies)
```

### Option 2: Scrapy-Splash (Local Proxy)
1. Run Splash container:
```bash
docker run -p 8050:8050 scrapinghub/splash
```

2. Modify parser.py to use Splash for requests

---

## Monitoring & Logs

### Docker Logs
```bash
docker-compose logs -f instagram-monitor-bot
```

### Systemd Logs
```bash
sudo journalctl -u instagram-monitor-bot -f -n 100
```

### Application Logs
Located at path specified by LOG_FILE environment variable (default: logs/bot.log)

---

## Troubleshooting

### Bot not starting
1. Check environment variables: `env | grep -E 'TELEGRAM|OPENAI|CLAUDE'`
2. Verify database initialization: `python3 db_init.py`
3. Test API keys manually

### Instagram scraping fails
1. Check Instagram credentials
2. Verify network connectivity
3. Consider using relay/proxy service
4. Check Instagram rate limits (max 200 requests/hour)

### High memory usage
1. Reduce POLL_INTERVAL
2. Reduce MAX_POSTS_PER_POLL
3. Clear logs directory periodically

---

## Maintenance

### Regular Tasks
- Monitor logs for errors
- Backup database (auto-backup to DATABASE_BACKUP_PATH)
- Update dependencies: `pip install --upgrade -r requirements.txt`
- Rotate logs: Configure logrotate on Linux

### Health Checks
Add cron job to verify bot is running:
```bash
# Check every 5 minutes
*/5 * * * * systemctl is-active --quiet instagram-monitor-bot || systemctl restart instagram-monitor-bot
```

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| TELEGRAM_BOT_TOKEN | Yes | - | Telegram Bot API token |
| OPENAI_API_KEY | Yes | - | OpenAI API key for filtering |
| CLAUDE_API_KEY | Yes | - | Anthropic Claude API key for analysis |
| INSTAGRAM_USERNAME | Yes | - | Instagram account username |
| INSTAGRAM_PASSWORD | Yes | - | Instagram account password |
| DATABASE_PATH | No | monitor.db | SQLite database path |
| DATABASE_BACKUP_PATH | No | backups/ | Backup directory path |
| LOG_LEVEL | No | INFO | Logging level (DEBUG/INFO/WARNING/ERROR) |
| LOG_FILE | No | bot.log | Log file path |
| POLL_INTERVAL | No | 300 | Polling interval in seconds |
| MAX_POSTS_PER_POLL | No | 10 | Max posts to process per poll |
| SENTIMENT_THRESHOLD | No | 0.7 | Sentiment threshold for alerts |
| NOTIFY_ON_NEW_POSTS | No | true | Send notifications for new posts |
| NOTIFY_ON_HIGH_ENGAGEMENT | No | true | Send notifications for high engagement |
| NOTIFY_ON_SENTIMENT_ALERTS | No | true | Send notifications for sentiment alerts |
