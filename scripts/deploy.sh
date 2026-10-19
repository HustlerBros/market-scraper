#!/bin/sh
# Remote deployment script executed via SSH on remote server
set -eu

echo "Deploying MEXC Futures Scraper and Prometheus..."

# Ensure the script executes from the project root on the remote server
deploy_dir="${DEPLOY_DIR:-"$HOME/market-scraper"}"
if [ ! -d "$deploy_dir" ]; then
    echo "Error: Deployment directory '$deploy_dir' not found." >&2
    exit 1
fi
cd "$deploy_dir"

# If DEPLOY_SHA is provided by CI, fetch and checkout that exact commit
if [ -n "${DEPLOY_SHA:-}" ]; then
    echo "Fetching and checking out commit $DEPLOY_SHA..."
    git fetch origin "$DEPLOY_SHA"
    git checkout -q "$DEPLOY_SHA"
fi

# Validate that the Telegram Bot Token is available either in .env or the environment
if [ ! -f ".env" ] && [ -z "${TG_BOT_TOKEN:-}" ]; then
    echo "Error: .env file not found and TG_BOT_TOKEN environment variable is not set." >&2
    echo "Please create a .env file or export TG_BOT_TOKEN before deploying." >&2
    exit 1
fi

# Detect Docker Compose implementation
if docker compose version >/dev/null 2>&1; then
    COMPOSE_MODE="plugin"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_MODE="standalone"
else
    echo "Error: Neither 'docker compose' nor 'docker-compose' was found on your system." >&2
    exit 1
fi

compose() {
    if [ "$COMPOSE_MODE" = "plugin" ]; then
        docker compose "$@"
    else
        docker-compose "$@"
    fi
}

echo "Building services..."
compose build

echo "Starting services..."
compose up -d --remove-orphans

echo "Verifying service health..."
container_id=$(compose ps -q scraper)
if [ -z "$container_id" ]; then
    echo "Error: Scraper container failed to start." >&2
    compose logs --tail 50 scraper >&2
    exit 1
fi

# Wait for scanner healthcheck (up to 180 seconds: 36 attempts * 5s)
attempt=0
max_attempts=36
while [ "$attempt" -lt "$max_attempts" ]; do
    status=$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || echo "unknown")
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || echo "unknown")

    if [ "$status" != "running" ]; then
        echo "Container stopped unexpectedly (status: $status)." >&2
        compose logs --tail 50 scraper >&2
        exit 1
    fi

    if [ "$health" = "healthy" ]; then
        echo "Deployment complete! Scraper is healthy."
        compose ps
        exit 0
    fi

    if [ "$health" = "unhealthy" ]; then
        echo "Health check failed: scraper is unhealthy." >&2
        compose logs --tail 50 scraper >&2
        exit 1
    fi

    attempt=$((attempt + 1))
    sleep 5
done

echo "Error: Scraper did not become healthy within 180 seconds." >&2
compose logs --tail 50 scraper >&2
exit 1
