#!/usr/bin/env bash
set -Eeuo pipefail

[[ ${EUID} -eq 0 ]] || { echo 'Run this deployment as root.' >&2; exit 1; }
for executable in docker curl openssl systemctl; do
  command -v "$executable" >/dev/null || { echo "Missing command: $executable" >&2; exit 1; }
done
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PANEL_DOMAIN="${PANEL_DOMAIN:?Set PANEL_DOMAIN to the dashboard hostname}"
XRAY_PUBLIC_HOST="${XRAY_PUBLIC_HOST:?Set XRAY_PUBLIC_HOST to the new server IP or hostname}"
[[ "$PANEL_DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || exit 1
PANEL_ENV=/etc/xray-panel.env
if [[ -f "$PANEL_ENV" ]]; then
  # Preserve the private runtime file on upgrades; environment values are not shell source.
  echo 'Reusing existing dashboard credentials.'
else
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
  if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
    read -rsp 'Dashboard password (at least 12 characters): ' ADMIN_PASSWORD
    echo
  fi
  [[ ${#ADMIN_PASSWORD} -ge 12 ]] || { echo 'Password is too short.' >&2; exit 1; }
  [[ "$ADMIN_USERNAME$ADMIN_PASSWORD" != *$'\n'* && "$ADMIN_USERNAME$ADMIN_PASSWORD" != *$'\r'* ]] || exit 1
fi
cd "$ROOT_DIR"
image="${PANEL_IMAGE:-xray-monitor-panel:$(date +%Y%m%d%H%M%S)}"
if [[ -n "${PANEL_IMAGE:-}" ]]; then
  docker image inspect "$image" >/dev/null
else
  DOCKER_BUILDKIT=0 docker build --network host -t "$image" .
fi

collector_log="$(mktemp)"
trap 'unlink "$collector_log"' EXIT
if ! MONITOR_API_DOMAIN="$PANEL_DOMAIN" XRAY_PUBLIC_HOST="$XRAY_PUBLIC_HOST" \
  bash scripts/install-collector.sh >"$collector_log" 2>&1; then
  sed -E 's/^(MONITOR_TOKEN=).*/\1[redacted]/' "$collector_log" >&2
  exit 1
fi
token="$(sed -n 's/^XRAY_MONITOR_TOKEN=//p' /etc/xray-monitor.env)"
if [[ ! -f "$PANEL_ENV" ]]; then
  umask 077
  {
    printf 'MONITOR_ENDPOINT=http://127.0.0.1:8787/v1/snapshot\n'
    printf 'MONITOR_TOKEN=%s\n' "$token"
    printf 'ADMIN_USERNAME=%s\nADMIN_PASSWORD=%s\n' "$ADMIN_USERNAME" "$ADMIN_PASSWORD"
    printf 'SESSION_SECRET=%s\n' "$(openssl rand -hex 32)"
  } >"$PANEL_ENV"
fi
chmod 600 "$PANEL_ENV"

previous=""
if docker container inspect xray-monitor-panel >/dev/null 2>&1; then
  previous="xray-monitor-panel-previous-$(date +%Y%m%d%H%M%S)"
  docker stop xray-monitor-panel >/dev/null
  docker rename xray-monitor-panel "$previous"
fi
docker run -d --name xray-monitor-panel --restart unless-stopped --network host \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 128 --memory 512m \
  --env-file "$PANEL_ENV" --env NODE_OPTIONS=--max-old-space-size=256 \
  --log-opt max-size=10m --log-opt max-file=3 "$image" >/dev/null
healthy=false
for _ in {1..20}; do
  if curl -fsS http://127.0.0.1:3000/login >/dev/null 2>&1; then healthy=true; break; fi
  sleep 1
done
if [[ "$healthy" != true ]]; then
  docker logs --tail 30 xray-monitor-panel >&2
  if [[ -n "$previous" ]]; then
    docker stop xray-monitor-panel >/dev/null
    docker rename xray-monitor-panel "xray-monitor-panel-failed-$(date +%s)"
    docker rename "$previous" xray-monitor-panel
    docker start xray-monitor-panel >/dev/null
  fi
  exit 1
fi

site=/etc/caddy/sites/xray-monitor-api.conf
site_backup="${site}.before-self-hosted-$(date +%Y%m%d%H%M%S)"
cp -a "$site" "$site_backup"
cat >"$site" <<EOF
${PANEL_DOMAIN} {
    encode zstd gzip
    header {
        X-Content-Type-Options nosniff
        Referrer-Policy same-origin
        X-Frame-Options DENY
    }
    @collector path /v1/*
    respond @collector 404
    reverse_proxy 127.0.0.1:3000
}
EOF
if ! caddy validate --config /etc/caddy/Caddyfile; then
  cp -a "$site_backup" "$site"
  exit 1
fi
if ! { systemctl reload caddy || systemctl restart caddy; }; then
  cp -a "$site_backup" "$site"
  systemctl restart caddy || true
  exit 1
fi
echo "Dashboard running at https://${PANEL_DOMAIN}/login"
