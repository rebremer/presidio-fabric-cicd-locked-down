#!/usr/bin/env bash
# Bootstrap an Ubuntu VM as an Azure DevOps self-hosted agent for the
# `deploy-presidio-fabric` pipeline.
#
# Run on the agent VM (e.g. `test-fabricjumphost-linux-vm`) as a sudoer:
#
#   git clone https://dev.azure.com/renebremer/test-presidio-cicd-privenv/_git/test-presidio-cicd-privenv
#   cd test-presidio-cicd-privenv
#   ORG_URL=https://dev.azure.com/renebremer PAT=<pat> ./scripts/setup-agent.sh
#
# The script installs:
#   1. apt build deps (gcc, g++, curl, jq, unzip)
#   2. Miniconda  (so we can mirror the Fabric runtime env locally)
#   3. Azure CLI  (required by the AzureCLI@2 pipeline task)
#   4. The ADO agent, registered as a systemd service, in the chosen pool
#
# Env vars (all optional except ORG_URL + PAT):
#   ORG_URL     Azure DevOps org URL, e.g. https://dev.azure.com/renebremer
#   PAT         PAT with scope "Agent Pools (Read & manage)"
#   POOL        Default
#   AGENT_NAME  $(hostname)
#   WORK_DIR    $HOME/agent
#   AGENT_VER   3.243.1

set -euo pipefail

: "${ORG_URL:?ORG_URL is required (e.g. https://dev.azure.com/renebremer)}"
: "${PAT:?PAT is required}"
POOL="${POOL:-Default}"
AGENT_NAME="${AGENT_NAME:-$(hostname)}"
WORK_DIR="${WORK_DIR:-$HOME/agent}"
AGENT_VER="${AGENT_VER:-3.243.1}"

AGENT_URL="https://download.agent.dev.azure.com/agent/${AGENT_VER}/vsts-agent-linux-x64-${AGENT_VER}.tar.gz"

echo "[1/4] apt prerequisites..."
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    gcc g++ make curl wget jq unzip git ca-certificates libicu-dev

echo "[2/4] Miniconda..."
if [ ! -d "$HOME/miniconda3" ]; then
    wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh
    bash /tmp/miniconda.sh -b -p "$HOME/miniconda3"
    rm /tmp/miniconda.sh
fi
# Make conda visible to interactive + non-interactive shells (ADO uses non-interactive)
if ! grep -q 'miniconda3/bin' "$HOME/.bashrc"; then
    echo 'export PATH="$HOME/miniconda3/bin:$PATH"' >> "$HOME/.bashrc"
fi
export PATH="$HOME/miniconda3/bin:$PATH"
conda --version

echo "[3/4] Azure CLI..."
if ! command -v az >/dev/null 2>&1; then
    curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
fi
az --version | head -n 1

echo "[4/4] ADO agent..."
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"
if [ ! -f "$WORK_DIR/config.sh" ]; then
    curl -sSL "$AGENT_URL" -o agent.tar.gz
    tar zxf agent.tar.gz
    rm agent.tar.gz
fi

# Stop+remove any existing service so this script is idempotent
if [ -f "$WORK_DIR/svc.sh" ]; then
    sudo "$WORK_DIR/svc.sh" stop || true
    sudo "$WORK_DIR/svc.sh" uninstall || true
    "$WORK_DIR/config.sh" remove --unattended --auth pat --token "$PAT" || true
fi

./config.sh \
    --unattended \
    --url "$ORG_URL" \
    --auth pat \
    --token "$PAT" \
    --pool "$POOL" \
    --agent "$AGENT_NAME" \
    --acceptTeeEula \
    --replace

# Install the agent as a systemd service running under the current user so it
# inherits ~/miniconda3 on PATH.
sudo ./svc.sh install "$USER"
sudo ./svc.sh start

echo
echo "Agent '$AGENT_NAME' is online in pool '$POOL'."
echo "Verify in: Org Settings -> Agent pools -> $POOL -> Agents tab."
echo "You can now revoke the PAT used for registration."
