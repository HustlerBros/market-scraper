#!/bin/bash
set -e

echo "Deploying MEXC Futures Scraper and Prometheus..."

# Ensure the script executes from the project root (where docker-compose.yml is located)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Validate that the Telegram Bot Token is available either in .env or the environment
if [ ! -f ".env" ] && [ -z "$TG_BOT_TOKEN" ]; then
    echo "X Error: .env file not found and TG_BOT_TOKEN environment variable is not set."
    echo "Please create a .env file containing your TG_BOT_TOKEN before deploying."
    exit 1
fi

# Detect Docker Compose version
if command -v docker compose &> /dev/null; then
    DOCKER_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    DOCKER_CMD="docker-compose"
else
    echo "X Error: Neither 'docker compose' nor 'docker-compose' was found on your system."
    exit 1
fi

echo "Stopping existing containers..."
$DOCKER_CMD down

echo "Building and starting services..."
$DOCKER_CMD up -d --build

echo "Deployment complete! Current service status:"
$DOCKER_CMD ps
