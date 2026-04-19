#!/bin/bash
# =============================================================================
# Polymarket Trading Bot - VPS Deployment Script
# =============================================================================
#
# This script sets up the trading bot on a fresh Ubuntu VPS.
#
# Prerequisites:
# - Ubuntu 20.04+ VPS (DigitalOcean, AWS, etc.)
# - SSH access to the VPS
# - Git repository URL
#
# Usage:
#   1. SSH into your VPS: ssh root@your-vps-ip
#   2. Run: curl -sSL https://raw.githubusercontent.com/YOUR_REPO/main/scripts/bash/deploy.sh | bash
#   Or:
#   3. Copy this script to VPS and run: chmod +x deploy.sh && ./deploy.sh
#
# =============================================================================

set -e  # Exit on error

echo "========================================"
echo "Polymarket Trading Bot - VPS Setup"
echo "========================================"

# Configuration
REPO_URL="${REPO_URL:-https://github.com/goldenAIge/PB-J.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/polymarket-bot}"
SERVICE_USER="${SERVICE_USER:-polybot}"
PYTHON_VERSION="3.11"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    log_error "Please run as root (use sudo)"
    exit 1
fi

# Update system
log_info "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq

# Install dependencies
log_info "Installing dependencies..."
apt-get install -y -qq \
    python${PYTHON_VERSION} \
    python${PYTHON_VERSION}-venv \
    python${PYTHON_VERSION}-pip \
    python3-pip \
    git \
    curl \
    htop \
    screen \
    tmux \
    supervisor \
    ufw

# Create service user if doesn't exist
if ! id "$SERVICE_USER" &>/dev/null; then
    log_info "Creating service user: $SERVICE_USER"
    useradd -r -s /bin/bash -m -d /home/$SERVICE_USER $SERVICE_USER
fi

# Clone or update repository
if [ -d "$INSTALL_DIR" ]; then
    log_info "Updating existing installation..."
    cd $INSTALL_DIR
    sudo -u $SERVICE_USER git pull
else
    log_info "Cloning repository..."
    git clone $REPO_URL $INSTALL_DIR
    chown -R $SERVICE_USER:$SERVICE_USER $INSTALL_DIR
fi

cd $INSTALL_DIR

# Set up Python virtual environment
log_info "Setting up Python virtual environment..."
sudo -u $SERVICE_USER python${PYTHON_VERSION} -m venv .venv
sudo -u $SERVICE_USER .venv/bin/pip install --upgrade pip
sudo -u $SERVICE_USER .venv/bin/pip install -r requirements.txt 2>/dev/null || {
    log_warn "Some packages failed, installing core dependencies..."
    sudo -u $SERVICE_USER .venv/bin/pip install \
        python-dotenv pydantic httpx web3 \
        py-clob-client \
        python-telegram-bot newsapi-python \
        langchain langchain-openai chromadb typer devtools
}

# Create .env file if not exists
if [ ! -f "$INSTALL_DIR/.env" ]; then
    log_info "Creating .env file from template..."
    if [ -f "$INSTALL_DIR/.env.example" ]; then
        cp $INSTALL_DIR/.env.example $INSTALL_DIR/.env
        chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/.env
        chmod 600 $INSTALL_DIR/.env
        log_warn "Please edit $INSTALL_DIR/.env with your API keys!"
    fi
fi

# Create logs directory
mkdir -p $INSTALL_DIR/logs
chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/logs

# Create systemd service for directional trader
log_info "Creating systemd services..."
cat > /etc/systemd/system/polymarket-directional.service << EOF
[Unit]
Description=Polymarket Directional Trading Bot
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONPATH=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m agents.application.arbitrage_trader --dry-run --scan-interval 300
Restart=always
RestartSec=10
StandardOutput=append:$INSTALL_DIR/logs/directional.log
StandardError=append:$INSTALL_DIR/logs/directional_error.log

[Install]
WantedBy=multi-user.target
EOF

# Create systemd service for resolution scalper
cat > /etc/systemd/system/polymarket-scalper.service << EOF
[Unit]
Description=Polymarket Resolution Scalper Bot
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONPATH=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m agents.application.resolution_scalper --dry-run --scan-interval 120
Restart=always
RestartSec=10
StandardOutput=append:$INSTALL_DIR/logs/scalper.log
StandardError=append:$INSTALL_DIR/logs/scalper_error.log

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd
systemctl daemon-reload

# Set up firewall (optional)
log_info "Configuring firewall..."
ufw allow ssh
ufw --force enable

# Create management script
log_info "Creating management script..."
cat > /usr/local/bin/polybot << 'EOF'
#!/bin/bash
# Polymarket Bot Management Script

INSTALL_DIR="/opt/polymarket-bot"
cd $INSTALL_DIR

case "$1" in
    start-directional)
        echo "Starting directional trader..."
        sudo systemctl start polymarket-directional
        ;;
    stop-directional)
        echo "Stopping directional trader..."
        sudo systemctl stop polymarket-directional
        ;;
    start-scalper)
        echo "Starting resolution scalper..."
        sudo systemctl start polymarket-scalper
        ;;
    stop-scalper)
        echo "Stopping resolution scalper..."
        sudo systemctl stop polymarket-scalper
        ;;
    status)
        echo "=== Directional Trader ==="
        sudo systemctl status polymarket-directional --no-pager
        echo ""
        echo "=== Resolution Scalper ==="
        sudo systemctl status polymarket-scalper --no-pager
        ;;
    logs-directional)
        tail -f $INSTALL_DIR/logs/directional.log
        ;;
    logs-scalper)
        tail -f $INSTALL_DIR/logs/scalper.log
        ;;
    scan)
        source $INSTALL_DIR/.venv/bin/activate
        PYTHONPATH=$INSTALL_DIR python scripts/python/cli.py scan-directional
        ;;
    balance)
        source $INSTALL_DIR/.venv/bin/activate
        PYTHONPATH=$INSTALL_DIR python scripts/python/cli.py check-balance
        ;;
    update)
        echo "Updating bot..."
        cd $INSTALL_DIR
        git pull
        source .venv/bin/activate
        pip install -r requirements.txt
        sudo systemctl restart polymarket-directional
        sudo systemctl restart polymarket-scalper
        ;;
    edit-env)
        sudo nano $INSTALL_DIR/.env
        ;;
    *)
        echo "Polymarket Bot Management"
        echo ""
        echo "Usage: polybot <command>"
        echo ""
        echo "Commands:"
        echo "  start-directional  Start directional trader"
        echo "  stop-directional   Stop directional trader"
        echo "  start-scalper      Start resolution scalper"
        echo "  stop-scalper       Stop resolution scalper"
        echo "  status             Show service status"
        echo "  logs-directional   View directional logs"
        echo "  logs-scalper       View scalper logs"
        echo "  scan               Run directional scan"
        echo "  balance            Check wallet balance"
        echo "  update             Update bot from git"
        echo "  edit-env           Edit .env file"
        ;;
esac
EOF
chmod +x /usr/local/bin/polybot

echo ""
echo "========================================"
echo "Installation Complete!"
echo "========================================"
echo ""
log_info "Installation directory: $INSTALL_DIR"
log_info "Service user: $SERVICE_USER"
echo ""
log_warn "IMPORTANT: Edit your .env file with API keys:"
echo "  sudo nano $INSTALL_DIR/.env"
echo ""
log_info "Management commands:"
echo "  polybot start-directional  # Start directional trader (dry-run)"
echo "  polybot start-scalper      # Start resolution scalper (dry-run)"
echo "  polybot status             # Check status"
echo "  polybot logs-directional   # View logs"
echo "  polybot balance            # Check wallet balance"
echo ""
log_warn "For LIVE trading, edit the systemd service files to remove --dry-run"
echo ""
