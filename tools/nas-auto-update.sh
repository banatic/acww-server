#!/bin/sh
# ACWW online server -- automatic update for a Synology NAS (DSM Task Scheduler, run as root).
#
# Every run:
#   1. asks GitHub for the newest commit of the public repository banatic/acww-server
#      (no token needed) and stops if it is the one already deployed;
#   2. asks the running server (/v1/health on the NAS itself) whether anyone is in the lobby
#      or in a multiplayer room, and stops if so -- the next run tries again;
#   3. downloads that exact commit, replaces ONLY app/, Dockerfile, requirements.txt and
#      .dockerignore in the project folder (data/ and docker-compose.yml are never touched),
#      keeping the previous copies;
#   4. rebuilds and restarts the same Container Manager project, and waits for /v1/health;
#   5. if the new server does not come up, puts the previous files back and rebuilds again.
#
# It changes nothing on the NAS except the project folder and the container. Turn the task
# off and everything stays exactly as it is. Its log is update.log in the project folder.

PATH="${ACWW_TEST_PATH:+$ACWW_TEST_PATH:}/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"   # Task Scheduler's PATH is short

# Edit these if your NAS differs. (The ACWW_* overrides exist only for the test harness.)
PROJECT_DIR="${ACWW_PROJECT_DIR:-/volume1/docker/acww-online}"   # Container Manager's project folder
REPO="banatic/acww-server"
BRANCH="main"
HEALTH="${ACWW_HEALTH_URL:-http://127.0.0.1:8080/v1/health}"     # the NAS-local port from docker-compose.yml
CONTAINER="acww-online"                                          # container_name in docker-compose.yml

LOG="$PROJECT_DIR/update.log"
STAMP="$PROJECT_DIR/.deployed_commit"
LOCK="${ACWW_LOCK_DIR:-/tmp/acww-auto-update.lock}"
WORK="${ACWW_WORK_DIR:-/tmp/acww-auto-update}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

trim_log() {
    if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 1000 ]; then
        tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
}

health_ok() {
    curl -fsS --max-time 5 "$HEALTH" 2>/dev/null | grep -q '"ok":true'
}

compose() {
    # Container Manager names the project; rebuilding under any other name would collide
    # with the fixed container_name. Ask the running container which project it belongs to.
    project=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$CONTAINER" 2>/dev/null)
    [ -n "$project" ] || project=$(basename "$PROJECT_DIR")
    if docker compose version >/dev/null 2>&1; then
        docker compose -p "$project" -f "$PROJECT_DIR/docker-compose.yml" --project-directory "$PROJECT_DIR" "$@"
    else
        docker-compose -p "$project" -f "$PROJECT_DIR/docker-compose.yml" --project-directory "$PROJECT_DIR" "$@"
    fi
}

wait_healthy() {
    i=0
    while [ $i -lt 24 ]; do            # up to two minutes
        sleep 5
        health_ok && return 0
        i=$((i + 1))
    done
    return 1
}

# One run at a time. A lock left by a run that was killed half way is stale after an hour
# (a normal run takes a few minutes), so it can never block every later run.
find "$LOCK" -maxdepth 0 -mmin +60 -exec rm -rf {} \; 2>/dev/null
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rm -rf "$LOCK" "$WORK"' EXIT
trim_log

# 1. The newest commit on GitHub.
latest=$(curl -fsS --max-time 20 -H "Accept: application/vnd.github.sha" \
    "https://api.github.com/repos/$REPO/commits/$BRANCH" 2>/dev/null)
case "$latest" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
    *) log "skip: GitHub did not answer with a commit"; exit 0 ;;
esac
current=$(cat "$STAMP" 2>/dev/null)
[ "$latest" = "$current" ] && exit 0

# 2. Nobody in the lobby or in a room.
status=$(curl -fsS --max-time 5 "$HEALTH" 2>/dev/null)
waiting=$(echo "$status" | sed -n 's/.*"waiting":\([0-9]*\).*/\1/p')
rooms=$(echo "$status" | sed -n 's/.*"rooms":\([0-9]*\).*/\1/p')
if [ -n "$status" ] && { [ "${waiting:-0}" != "0" ] || [ "${rooms:-0}" != "0" ]; }; then
    log "wait: $latest is ready but the server is busy (waiting=$waiting rooms=$rooms)"
    exit 0
fi

# 3. Download that exact commit and swap the code files.
rm -rf "$WORK" && mkdir -p "$WORK" || exit 1
if ! curl -fsSL --max-time 120 -o "$WORK/src.tar.gz" \
        "https://codeload.github.com/$REPO/tar.gz/$latest"; then
    log "fail: download of $latest"; exit 0
fi
if ! tar -xzf "$WORK/src.tar.gz" -C "$WORK"; then
    log "fail: could not unpack $latest"; exit 0
fi
src=$(find "$WORK" -mindepth 1 -maxdepth 1 -type d | head -n 1)
if [ ! -f "$src/app/main.py" ] || [ ! -f "$src/Dockerfile" ] || [ ! -f "$src/requirements.txt" ]; then
    log "fail: $latest does not look like the server"; exit 0
fi

cd "$PROJECT_DIR" || exit 1
rm -rf app.prev && cp -a app app.prev
for f in Dockerfile requirements.txt .dockerignore; do
    [ -f "$f" ] && cp -a "$f" "$f.prev"
done
rm -rf app.new && cp -a "$src/app" app.new && rm -rf app && mv app.new app
for f in Dockerfile requirements.txt .dockerignore; do
    [ -f "$src/$f" ] && cp "$src/$f" "$f"
done
# The container runs as uid 10001 and only needs to read the code.
chmod -R a+rX app Dockerfile requirements.txt .dockerignore 2>/dev/null

# 4. Rebuild and restart the same project.
log "deploy: $current -> $latest"
# `up --build` rebuilds the image under the same tag and, because the image changed,
# replaces the container -- the step that used to be "delete the container, build, start".
if compose up -d --build >> "$LOG" 2>&1 && wait_healthy; then
    echo "$latest" > "$STAMP"
    log "ok: $(curl -fsS --max-time 5 "$HEALTH" 2>/dev/null)"
    # The replaced image is left untagged (<none>); drop untagged images so they do not pile
    # up. Tagged images and every container are left alone.
    docker image prune -f >> "$LOG" 2>&1
    exit 0
fi

# 5. It did not come up: put the previous files back and rebuild.
log "fail: $latest did not come up healthy; restoring the previous files"
rm -rf app && mv app.prev app
for f in Dockerfile requirements.txt .dockerignore; do
    [ -f "$f.prev" ] && mv "$f.prev" "$f"
done
if compose up -d --build >> "$LOG" 2>&1 && wait_healthy; then
    log "restored: the previous server is running again"
else
    log "ALERT: the previous server did not come back either; check Container Manager"
fi
# Remember the bad commit so the next run does not retry it; a newer commit replaces it.
echo "$latest" > "$STAMP"
exit 0
