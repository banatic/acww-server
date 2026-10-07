#!/bin/sh
# Exercise nas-auto-update.sh against the real GitHub repository with a stub `docker` and a
# stub /v1/health. Needs sh, curl, tar and python3 (PY=python where python3 is a stub). No NAS, no Docker.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d)
PORT=$((20000 + $$ % 20000))
mkdir -p "$T/proj/app" "$T/proj/data" "$T/stub" "$T/web/v1"
echo "old" > "$T/proj/app/main.py"
echo "FROM old" > "$T/proj/Dockerfile"
echo "old==1" > "$T/proj/requirements.txt"
echo "services: {}" > "$T/proj/docker-compose.yml"
echo "keep" > "$T/proj/data/users.sqlite"

cat > "$T/stub/docker" <<'EOF'
#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  inspect) echo "acww-online" ;;
  compose) [ "$2" = "version" ] && exit 0; [ -f "$STUB_FAIL" ] && exit 1; exit 0 ;;
  image) exit 0 ;;
esac
EOF
chmod +x "$T/stub/docker"

health() { printf '{"ok":true,"version":"x","waiting":%s,"rooms":%s}' "$1" "$2" > "$T/web/v1/health"; }
${PY:-python3} -m http.server "$PORT" --bind 127.0.0.1 --directory "$T/web" >/dev/null 2>&1 &
SERVER=$!          # the python process itself, so kill stops it
i=0; until curl -fsS --max-time 2 "http://127.0.0.1:$PORT/" >/dev/null 2>&1 || [ $i -ge 20 ]; do sleep 0.5; i=$((i+1)); done

run() {
    ACWW_LOCK_DIR="$T/lock" ACWW_WORK_DIR="$T/work" ACWW_TEST_PATH="$T/stub" ACWW_PROJECT_DIR="$T/proj" STUB_LOG="$T/docker.log" STUB_FAIL="$T/fail" \
    ACWW_HEALTH_URL="http://127.0.0.1:$PORT/v1/health" sh "$HERE/nas-auto-update.sh"
}
fail() { echo "FAIL: $*"; cat "$T/proj/update.log" 2>/dev/null; kill $SERVER; rm -rf "$T"; exit 1; }

# 1. Busy: nothing changes.
health 1 0; run
grep -q '^wait:' "$T/proj/update.log" 2>/dev/null || grep -q ' wait: ' "$T/proj/update.log" || fail "busy run did not wait"
[ "$(cat "$T/proj/app/main.py")" = "old" ] || fail "busy run changed app"
[ -f "$T/proj/.deployed_commit" ] && fail "busy run stamped"

# 2. Idle: deploy.
health 0 0; run
[ -f "$T/proj/app/main.py" ] && [ "$(cat "$T/proj/app/main.py")" != "old" ] || fail "app not replaced"
grep -q "up -d --build" "$T/docker.log" || fail "compose up not called"
grep -q -- "-p acww-online" "$T/docker.log" || fail "project name not taken from the container"
grep -q "image prune -f" "$T/docker.log" || fail "prune not called"
[ "$(cat "$T/proj/data/users.sqlite")" = "keep" ] || fail "data touched"
[ "$(cat "$T/proj/docker-compose.yml")" = "services: {}" ] || fail "compose file touched"
first=$(cat "$T/proj/.deployed_commit")

# 3. Same commit again: nothing.
: > "$T/docker.log"; run
[ -s "$T/docker.log" ] && fail "second run did work"

# 4. A new commit that fails to come up: restored.
echo "old" > "$T/proj/app/main.py"; echo "stale" > "$T/proj/.deployed_commit"; touch "$T/fail"
run
grep -q "restoring the previous files" "$T/proj/update.log" || fail "no rollback"
[ "$(cat "$T/proj/app/main.py")" = "old" ] || fail "previous app not restored"

kill $SERVER; rm -rf "$T"
echo "nas-auto-update: PASS -- busy waits, idle deploys ($first), no-op on same commit, rollback on failure"
