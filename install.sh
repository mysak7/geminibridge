#!/usr/bin/env bash
# install.sh — Bootstrap geminibridge on a fresh Linux (Ubuntu/Debian) machine.
# Supports both GEMINI_API_KEY and browser OAuth authentication.
# Usage: sudo bash install.sh
set -euo pipefail

# ─── Colour helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ─── Require root ────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Run as root: sudo bash install.sh"

# ─── 1. Collect bridge API key ───────────────────────────────────────────────
info "Collecting configuration values."

if [[ -z "${API_KEY:-}" ]]; then
    read -rsp "  Bridge API key (choose any secret token callers must send): " API_KEY; echo
fi
[[ -n "$API_KEY" ]] || error "API_KEY is required."

read -rp "  Port to expose the API on [8003]: " PORT
PORT="${PORT:-8003}"

# ─── 2. Install Node.js (LTS) ────────────────────────────────────────────────
if command -v node &>/dev/null; then
    info "Node.js already installed: $(node --version)"
else
    info "Installing Node.js LTS..."
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -
    apt-get install -y -qq nodejs
    info "Node.js installed: $(node --version)"
fi

# ─── 3. Install Gemini CLI on the host ───────────────────────────────────────
if command -v gemini &>/dev/null; then
    info "Gemini CLI already installed: $(gemini --version 2>/dev/null || echo 'version unknown')"
else
    info "Installing Gemini CLI..."
    npm install -g @google/gemini-cli
    info "Gemini CLI installed."
fi

# ─── 4. Gemini credentials ───────────────────────────────────────────────────
REAL_USER="${SUDO_USER:-root}"
REAL_HOME=$(eval echo "~${REAL_USER}")
GEMINI_CONFIG_DIR="${REAL_HOME}/.gemini"

echo ""
info "Gemini authentication setup."
echo ""

if [[ -n "${GEMINI_API_KEY:-}" ]]; then
    info "GEMINI_API_KEY is set — will use API key authentication."
    AUTH_METHOD="apikey"
else
    # Check for existing OAuth credentials
    if [[ -f "${GEMINI_CONFIG_DIR}/credentials.json" ]] || \
       [[ -f "${GEMINI_CONFIG_DIR}/oauth_creds.json" ]]; then
        warn "Existing Gemini OAuth credentials found at ${GEMINI_CONFIG_DIR} — skipping login."
        warn "To re-authenticate: gemini auth logout && gemini auth login"
        AUTH_METHOD="oauth"
    else
        echo "  Choose authentication method:"
        echo "  1) GEMINI_API_KEY  (recommended for servers)"
        echo "  2) Google OAuth    (browser login)"
        read -rp "  Enter 1 or 2 [1]: " AUTH_CHOICE
        AUTH_CHOICE="${AUTH_CHOICE:-1}"

        if [[ "$AUTH_CHOICE" == "1" ]]; then
            read -rsp "  Enter your GEMINI_API_KEY: " GEMINI_API_KEY; echo
            [[ -n "$GEMINI_API_KEY" ]] || error "GEMINI_API_KEY is required."
            AUTH_METHOD="apikey"
        else
            AUTH_METHOD="oauth"
            IS_HEADLESS=false
            if [[ -z "${DISPLAY:-}" ]] && [[ -z "${WAYLAND_DISPLAY:-}" ]]; then
                IS_HEADLESS=true
            fi

            if $IS_HEADLESS; then
                echo -e "${YELLOW}  Headless machine detected — browser cannot open automatically.${NC}"
                echo ""
                echo "  Copy your Gemini credentials from your LOCAL machine to this server:"
                echo ""
                echo "    scp -r ~/.gemini ${REAL_USER}@<this-server-ip>:~/.gemini"
                echo ""
                echo "  Run that command in a new terminal on your local machine,"
                echo "  then come back here and press Enter to continue."
                echo ""
                read -rp "  Press Enter when done: "
            else
                info "Starting Gemini CLI browser authentication..."
                sudo -u "$REAL_USER" gemini auth login || true
            fi
        fi
    fi
fi

# ─── 5. Install Docker ───────────────────────────────────────────────────────
if command -v docker &>/dev/null; then
    info "Docker already installed: $(docker --version)"
else
    info "Installing Docker..."
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg lsb-release

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo "deb [arch=$(dpkg --print-architecture) \
signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu \
$(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    info "Docker installed: $(docker --version)"
fi

# ─── 6. Locate project files ─────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Project directory: $SCRIPT_DIR"

for f in Dockerfile api.py requirements.txt; do
    [[ -f "$SCRIPT_DIR/$f" ]] || error "Missing required file: $f (run from the geminibridge repo root)"
done

# ─── 7. Build Docker image ───────────────────────────────────────────────────
IMAGE_NAME="geminibridge"
info "Building Docker image '$IMAGE_NAME'..."
docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"

# ─── 8. Write environment file ───────────────────────────────────────────────
ENV_FILE="$SCRIPT_DIR/.env"
info "Writing config to $ENV_FILE"
cat > "$ENV_FILE" <<EOF
API_KEY=${API_KEY}
GEMINI_API_KEY=${GEMINI_API_KEY:-}
EOF
chmod 600 "$ENV_FILE"

# ─── 9. Stop any existing container ──────────────────────────────────────────
if docker ps -aq --filter "name=^${IMAGE_NAME}$" | grep -q .; then
    warn "Stopping existing container '$IMAGE_NAME'..."
    docker stop "$IMAGE_NAME" 2>/dev/null || true
    docker rm   "$IMAGE_NAME" 2>/dev/null || true
fi

# ─── 10. Run the container ───────────────────────────────────────────────────
DATA_DIR="/home/${REAL_USER}/geminibridge"
mkdir -p "$DATA_DIR"

info "Starting container on port $PORT..."
EXTRA_MOUNTS=""
if [[ "$AUTH_METHOD" == "oauth" ]] && [[ -d "${GEMINI_CONFIG_DIR}" ]]; then
    EXTRA_MOUNTS="-v ${GEMINI_CONFIG_DIR}:/home/node/.gemini:ro"
fi

docker run -d \
    --name "$IMAGE_NAME" \
    --restart unless-stopped \
    -p "${PORT}:8000" \
    --env-file "$ENV_FILE" \
    -v "${DATA_DIR}:/data" \
    $EXTRA_MOUNTS \
    "$IMAGE_NAME"

# ─── 11. Health check ────────────────────────────────────────────────────────
info "Waiting for API to come up..."
for i in $(seq 1 15); do
    if curl -sf "http://localhost:${PORT}/docs" &>/dev/null; then break; fi
    sleep 2
done

if curl -sf "http://localhost:${PORT}/docs" &>/dev/null; then
    info "API is up at http://localhost:${PORT}"
else
    warn "API did not respond in time — check logs: docker logs $IMAGE_NAME"
fi

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  geminibridge installed successfully             ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Endpoint : http://localhost:${PORT}/v1/chat/completions"
echo "  Auth     : Bearer <your API_KEY>"
echo "  Logs     : docker logs -f $IMAGE_NAME"
echo ""
if [[ "$AUTH_METHOD" == "apikey" ]]; then
    echo "  Auth     : GEMINI_API_KEY (set in ${ENV_FILE})"
else
    echo "  Auth     : Google OAuth credentials from ${GEMINI_CONFIG_DIR}"
    echo "  If your session expires, run 'gemini auth login' on the host and restart:"
    echo "    docker restart $IMAGE_NAME"
fi
echo ""
echo "  Quick test:"
echo "    curl -s http://localhost:${PORT}/v1/chat/completions \\"
echo "      -H 'Authorization: Bearer \$API_KEY' \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"prompt\": \"Reply with: hello world\"}' | python3 -m json.tool"
echo ""
