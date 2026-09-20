#!/bin/sh
# Executed remotely by ssh-action; only replace this application's container.
set -eu

: "${DEPLOY_SHA:?DEPLOY_SHA must be set}"
: "${TG_BOT_TOKEN:?TG_BOT_TOKEN must be set}"
export TG_BOT_TOKEN

deploy_dir=${DEPLOY_DIR:-"$HOME/market-scraper"}
cd "$deploy_dir"
git fetch origin "$DEPLOY_SHA"
git cat-file -e "$DEPLOY_SHA^{commit}"

container=mexc-market-scraper-container
previous_container=mexc-market-scraper-previous
image="mexc-market-scraper:$DEPLOY_SHA"
archive=$(mktemp)
previous_saved=false
old_stopped=false
new_attempted=false
completed=false

# Invoked indirectly by the EXIT trap; rollback paths are covered by tests.
# shellcheck disable=SC2317
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ "$completed" = false ]; then
        if [ "$new_attempted" = true ]; then
            docker logs --tail 60 "$container" >&2 || true
            docker rm -f "$container" || true
        fi
        if [ "$previous_saved" = true ]; then
            docker rename "$previous_container" "$container" || true
        fi
        if [ "$old_stopped" = true ]; then
            docker start "$container" || true
            echo 'Deployment failed; attempted to restore the previous container.' >&2
        fi
    fi
    rm -f "$archive"
    exit "$result"
}
trap 'cleanup' EXIT
trap 'exit 1' HUP INT TERM

# Build exactly the tested commit before stopping the running bot. The archive
# excludes server-local changes and secrets from the Docker build context.
git archive --format=tar --output="$archive" "$DEPLOY_SHA"
docker build --tag "$image" - < "$archive"
docker run --rm --entrypoint python "$image" -c 'import src.mexc_futures_scraper'
mkdir -p "$deploy_dir/data"

if docker container inspect "$previous_container" >/dev/null 2>&1; then
    echo "A previous rollback container already exists: $previous_container. Resolve it before deploying." >&2
    exit 1
fi
if docker container inspect "$container" >/dev/null 2>&1; then
    old_stopped=true
    docker stop --time 30 "$container"
    docker rename "$container" "$previous_container"
    previous_saved=true
fi

new_attempted=true
docker run -d --name "$container" --restart unless-stopped \
    --mount "type=bind,src=$deploy_dir/data,dst=/app/data" \
    --env TG_BOT_TOKEN \
    --env MIN_GAIN_PERCENT \
    "$image"

# Health requires a completed market scan, including Telegram delivery attempts.
attempt=0
while [ "$attempt" -lt 36 ]; do
    state=$(docker inspect --format '{{.State.Status}}' "$container")
    health=$(docker inspect --format '{{.State.Health.Status}}' "$container")
    if [ "$state" != running ] || [ "$health" = unhealthy ]; then
        echo "Scanner failed startup: state=$state health=$health" >&2
        exit 1
    fi
    if [ "$health" = healthy ]; then
        completed=true
        if [ "$previous_saved" = true ]; then
            docker rm "$previous_container"
        fi
        echo "Deployed $DEPLOY_SHA: scanner is healthy."
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 5
done

echo 'Scanner did not become healthy within 180 seconds.' >&2
exit 1
