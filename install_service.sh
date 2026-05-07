#!/bin/bash

# Colour constants
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'   # no colour

LOGFILE="/var/log/jolly-relay.log"
CSVFILE="/var/log/jolly-relay-messages.csv"

# Ensure running as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}✗ Please run this script as root (e.g. using sudo).${NC}"
    exit 1
fi

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo -e "${GREEN}=============================================="
echo -e " Jolly Relay Service Installer"
echo -e "==============================================${NC}"
echo    "This script will:"
echo    "  1. Create a dedicated system user 'jolly-relay'"
echo    "  2. Create a virtual environment and install dependencies"
echo    "  3. Copy jolly-relay.yaml.example to /etc/postfix/jolly-relay.yaml (if missing)"
echo    "  4. Create and enable systemd service 'jolly-relay.service'"
echo -e "     running from: ${YELLOW}$DIR${NC}"
echo    ""
read -p "Do you want to proceed? [y/N]: " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "Installation aborted."
    exit 0
fi

echo -e "\n${YELLOW}[*] Creating system user 'jolly-relay'...${NC}"
if id "jolly-relay" &>/dev/null; then
    echo -e "    ${GREEN}✓${NC} User jolly-relay already exists."
else
    groupadd --system jolly-relay || true
    useradd --system --no-create-home --shell /usr/sbin/nologin -g jolly-relay jolly-relay || true
    echo -e "    ${GREEN}✓${NC} User and group jolly-relay created."
fi

echo -e "\n${YELLOW}[*] Modifying ownership for jolly-relay...${NC}"
chown -R jolly-relay:jolly-relay "$DIR"
echo -e "    ${GREEN}✓${NC} Ownership set."

echo -e "\n${YELLOW}[*] Creating log files...${NC}"
touch "$LOGFILE" "$CSVFILE"
chown jolly-relay:jolly-relay "$LOGFILE" "$CSVFILE"
chmod 666 "$LOGFILE" "$CSVFILE"
echo -e "    ${GREEN}✓${NC} $LOGFILE"
echo -e "    ${GREEN}✓${NC} $CSVFILE"

echo -e "\n${YELLOW}[*] Checking for python3 and compile dependencies...${NC}"
if ! command -v python3 &>/dev/null || ! command -v gcc &>/dev/null || ! ls /usr/include/python3*/Python.h &>/dev/null 2>/dev/null; then
    echo    "    python3, gcc or python3 headers are missing. Attempting to install..."
    if command -v apt-get &>/dev/null; then
        apt-get update && \
        DEBIAN_FRONTEND=noninteractive apt-get install -y python3 gcc && \
        DEBIAN_FRONTEND=noninteractive apt-get install -y python3-dev
    elif command -v dnf &>/dev/null; then
        dnf install -y python3 python3-devel gcc
    elif command -v yum &>/dev/null; then
        yum install -y python3 python3-devel gcc
    elif command -v zypper &>/dev/null; then
        zypper install -y python3 python3-devel gcc
    elif command -v pacman &>/dev/null; then
        pacman -Sy --noconfirm python gcc
    else
        echo -e "${YELLOW}    ⚠ Could not find a supported package manager."
        echo -e "    Please install python3, python3-dev and gcc manually.${NC}"
        read -p "    Press Enter to continue once installed, or Ctrl-C to abort."
    fi
fi

echo -e "\n${YELLOW}[*] Checking Python version...${NC}"
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(python3  -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3  -c 'import sys; print(sys.version_info.minor)')

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]; }; then
    echo -e "${RED}✗ Python $PY_VERSION detected. Jolly Relay requires Python 3.8 or later.${NC}"
    echo    ""
    echo    "  Three core dependencies (aiosmtpd, aiosmtplib, aiodns) have dropped"
    echo    "  Python 3.6 support in all current releases. Pinning old versions is"
    echo    "  not a safe option for a production mail relay."
    echo    ""
    echo -e "${YELLOW}  Recommended: install Python 3.11 alongside your system Python.${NC}"
    echo    "  Your system Python ($PY_VERSION) will not be touched."
    echo    ""
    echo    "  On Ubuntu 20.04 / 22.04 / Debian 11+:"
    echo    "    add-apt-repository ppa:deadsnakes/ppa"
    echo    "    apt-get install python3.11 python3.11-venv python3.11-dev"
    echo    ""
    echo -e "${YELLOW}  On Ubuntu 18.04 (deadsnakes no longer supports bionic):${NC}"
    echo    "  compile Python 3.11 from source into /opt (does not touch system Python):"
    echo    ""
    echo    "    apt-get install -y build-essential zlib1g-dev libncurses5-dev \\"
    echo    "      libgdbm-dev libnss3-dev libssl-dev libreadline-dev libffi-dev wget"
    echo    "    wget https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz"
    echo    "    tar xzf Python-3.11.9.tgz && cd Python-3.11.9"
    echo    "    ./configure --prefix=/opt/python3.11 --enable-optimizations"
    echo    "    make -j\$(nproc) && make install"
    echo    "    # Invoke as: /opt/python3.11/bin/python3.11"
    echo    ""
    echo    "  Then re-run this installer. It will detect the new interpreter"
    echo    "  automatically if python3.11 is in PATH, or set PYTHON3 to point to it:"
    echo    "    PYTHON3=/opt/python3.11/bin/python3.11 bash install_service.sh"
    exit 1
fi
echo -e "    ${GREEN}✓${NC} Python $PY_VERSION"

echo -e "\n${YELLOW}[*] Checking for venv support...${NC}"
if ! python3 -m venv --help &>/dev/null; then
    echo    "    python3-venv is not installed. Attempting to install..."
    if command -v apt-get &>/dev/null; then
        apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv "python${PY_VERSION}-venv"
    elif command -v dnf &>/dev/null; then
        dnf install -y python3-venv
    elif command -v yum &>/dev/null; then
        yum install -y python3-venv
    elif command -v zypper &>/dev/null; then
        zypper install -y python3-venv
    elif command -v pacman &>/dev/null; then
        pacman -Sy --noconfirm python
    else
        echo -e "${YELLOW}    ⚠ Could not find a supported package manager."
        echo -e "    Please install python3-venv manually.${NC}"
        read -p "    Press Enter to continue once installed, or Ctrl-C to abort."
    fi
fi
echo -e "    ${GREEN}✓${NC} venv available"

echo -e "\n${YELLOW}[*] Setting up virtual environment...${NC}"
PYTHON3="${PYTHON3:-python3}"
sudo -u jolly-relay "$PYTHON3" -m venv "$DIR/.venv"
sudo -u jolly-relay "$DIR/.venv/bin/pip" install --no-cache-dir --upgrade pip
sudo -u jolly-relay "$DIR/.venv/bin/pip" install --no-cache-dir -r "$DIR/requirements.txt"
echo -e "    ${GREEN}✓${NC} Virtual environment ready"

echo -e "\n${YELLOW}[*] Setting up configuration...${NC}"
if [ ! -d "/etc/postfix" ]; then
    mkdir -p /etc/postfix
fi
if [ ! -f "/etc/postfix/jolly-relay.yaml" ]; then
    cp "$DIR/jolly-relay.yaml.example" "/etc/postfix/jolly-relay.yaml"
    echo -e "    ${GREEN}✓${NC} Created /etc/postfix/jolly-relay.yaml from example."
else
    echo -e "    ${GREEN}✓${NC} /etc/postfix/jolly-relay.yaml already exists — left untouched."
fi

echo -e "\n${YELLOW}[*] Configuring local domains...${NC}"
if [ -f "/etc/postfix/virtual" ]; then
    echo    "    Found /etc/postfix/virtual. Enabling auto_populate_local_domains."
    sed -i 's/auto_populate_local_domains: false/auto_populate_local_domains: true/' /etc/postfix/jolly-relay.yaml
    sed -i -E 's|virtual_file: .*|virtual_file: /etc/postfix/virtual|' /etc/postfix/jolly-relay.yaml
    echo -e "    ${GREEN}✓${NC} auto_populate_local_domains enabled."
else
    echo    "    /etc/postfix/virtual not found — skipping auto-population."
fi

echo -e "\n${YELLOW}[*] Creating systemd service file...${NC}"
cat <<EOF > /etc/systemd/system/jolly-relay.service
[Unit]
Description=Jolly Relay SMTP Relay
After=network.target

[Service]
ExecStart=$DIR/.venv/bin/python $DIR/jolly-relay.py
WorkingDirectory=$DIR
Restart=on-failure
User=jolly-relay
Group=jolly-relay
StandardOutput=journal
StandardError=journal
SyslogIdentifier=jolly-relay
SyslogFacility=mail

[Install]
WantedBy=multi-user.target
EOF
echo -e "    ${GREEN}✓${NC} /etc/systemd/system/jolly-relay.service"

echo -e "\n${YELLOW}[*] Enabling and starting service...${NC}"
systemctl daemon-reload
systemctl enable jolly-relay
systemctl start jolly-relay
sleep 1

STATUS=$(systemctl is-active jolly-relay)
if [ "$STATUS" = "active" ]; then
    echo -e "    ${GREEN}✓${NC} jolly-relay is ${GREEN}active${NC}"
else
    echo -e "    ${RED}✗ jolly-relay status: $STATUS${NC}"
    echo    "    Check with: journalctl -u jolly-relay -n 30"
fi

echo ""
echo -e "${GREEN}=============================================="
echo -e " Installation complete!"
echo -e "==============================================${NC}"
echo    ""
echo    "  Logs:    journalctl -u jolly-relay -f"
echo    "  Config:  /etc/postfix/jolly-relay.yaml"
echo    ""
echo -e "${YELLOW}  Postfix integration:${NC}"
echo    ""
echo    "  Edit /etc/postfix/main.cf and add:"
echo    "    transport_maps = hash:/etc/postfix/transport"
echo    ""
echo    "  Create /etc/postfix/transport with entries like:"
echo    "    yourdomain.com   smtp:127.0.0.1:9725"
echo    "    .yourdomain.com  smtp:127.0.0.1:9725"
echo    ""
echo    "  Then run:"
echo    "    postmap /etc/postfix/transport && postfix reload"
echo    ""
