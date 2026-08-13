#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Install or update the Xray monitoring collector on a systemd server.

Required:
  MONITOR_API_DOMAIN   Public HTTPS domain for the collector API
  XRAY_PUBLIC_HOST     Host or IP used when the panel creates VLESS links

Example:
  sudo MONITOR_API_DOMAIN=monitor.example.com \
       XRAY_PUBLIC_HOST=proxy.example.com \
       bash scripts/install-collector.sh

Optional environment variables:
  MONITOR_TOKEN, XRAY_CONFIG, XRAY_CONF_DIR, XRAY_BIN, XRAY_API_PORT,
  XRAY_ACCESS_LOG, XRAY_MONITOR_INTERFACE, XRAY_MONITOR_MONTHLY_BYTES,
  XRAY_MONITOR_PERIOD_DAYS, XRAY_MONITOR_RESET_ANCHOR, CADDY_SITES_DIR
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run this installer as root (sudo)." >&2
  exit 1
fi

MONITOR_API_DOMAIN="${MONITOR_API_DOMAIN:-}"
XRAY_PUBLIC_HOST="${XRAY_PUBLIC_HOST:-}"
if [[ -z "${MONITOR_API_DOMAIN}" || -z "${XRAY_PUBLIC_HOST}" ]]; then
  usage >&2
  exit 1
fi
if [[ ! "${MONITOR_API_DOMAIN}" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "MONITOR_API_DOMAIN must contain only a hostname, without https:// or a path." >&2
  exit 1
fi
if [[ ! "${XRAY_PUBLIC_HOST}" =~ ^[A-Za-z0-9.:\[\]-]+$ ]]; then
  echo "XRAY_PUBLIC_HOST must be a hostname or IP address." >&2
  exit 1
fi

for command in python3 systemctl curl openssl install; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "Missing required command: ${command}" >&2
    exit 1
  fi
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
XRAY_CONFIG="${XRAY_CONFIG:-/etc/xray/config.json}"
XRAY_CONF_DIR="${XRAY_CONF_DIR:-/etc/xray/conf}"
XRAY_API_PORT="${XRAY_API_PORT:-47495}"
XRAY_ACCESS_LOG="${XRAY_ACCESS_LOG:-/var/log/xray/access.log}"
CADDY_SITES_DIR="${CADDY_SITES_DIR:-/etc/caddy/sites}"
ENV_FILE="/etc/xray-monitor.env"
if [[ -n "${XRAY_MONITOR_INTERFACE:-}" ]]; then
  NETWORK_INTERFACE="${XRAY_MONITOR_INTERFACE}"
elif command -v ip >/dev/null 2>&1; then
  NETWORK_INTERFACE="$(ip route show default 2>/dev/null | awk 'NR == 1 { print $5 }')"
else
  NETWORK_INTERFACE="eth0"
fi
NETWORK_INTERFACE="${NETWORK_INTERFACE:-eth0}"
XRAY_MONITOR_MONTHLY_BYTES="${XRAY_MONITOR_MONTHLY_BYTES:-536870912000}"
XRAY_MONITOR_PERIOD_DAYS="${XRAY_MONITOR_PERIOD_DAYS:-30}"
XRAY_MONITOR_RESET_ANCHOR="${XRAY_MONITOR_RESET_ANCHOR:-1970-01-01}"
if [[ ! "${XRAY_MONITOR_MONTHLY_BYTES}" =~ ^[1-9][0-9]*$ ]]; then
  echo "XRAY_MONITOR_MONTHLY_BYTES must be a positive integer." >&2
  exit 1
fi
if [[ ! "${XRAY_MONITOR_PERIOD_DAYS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "XRAY_MONITOR_PERIOD_DAYS must be a positive integer." >&2
  exit 1
fi
if [[ ! "${XRAY_MONITOR_RESET_ANCHOR}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "XRAY_MONITOR_RESET_ANCHOR must use YYYY-MM-DD." >&2
  exit 1
fi

if [[ -n "${XRAY_BIN:-}" ]]; then
  XRAY_BIN="${XRAY_BIN}"
elif command -v xray >/dev/null 2>&1; then
  XRAY_BIN="$(command -v xray)"
elif [[ -x /usr/local/bin/xray ]]; then
  XRAY_BIN="/usr/local/bin/xray"
else
  echo "Xray was not found. Install Xray before running this script." >&2
  exit 1
fi

if [[ ! -f "${XRAY_CONFIG}" ]]; then
  echo "Xray config not found: ${XRAY_CONFIG}" >&2
  exit 1
fi

if ! command -v caddy >/dev/null 2>&1 || [[ ! -f /etc/caddy/Caddyfile ]]; then
  echo "Caddy with /etc/caddy/Caddyfile is required for the HTTPS collector endpoint." >&2
  exit 1
fi
if ! grep -Eq '^[[:space:]]*import[[:space:]]+/etc/caddy/sites/' /etc/caddy/Caddyfile; then
  echo "Caddyfile must import /etc/caddy/sites/*.conf before installation." >&2
  exit 1
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
xray_backup="${XRAY_CONFIG}.before-xray-monitor.${timestamp}"
cp -a "${XRAY_CONFIG}" "${xray_backup}"

python3 "${ROOT_DIR}/scripts/patch-xray-config.py" \
  "${XRAY_CONFIG}" \
  --api-port "${XRAY_API_PORT}" \
  --access-log "${XRAY_ACCESS_LOG}"

test_args=(run -test -config "${XRAY_CONFIG}")
if [[ -d "${XRAY_CONF_DIR}" ]]; then
  test_args+=(-confdir "${XRAY_CONF_DIR}")
fi
if ! "${XRAY_BIN}" "${test_args[@]}"; then
  cp -a "${xray_backup}" "${XRAY_CONFIG}"
  echo "Xray validation failed; the original config was restored." >&2
  exit 1
fi

install -d -m 0755 /opt/xray-monitor /var/lib/xray-monitor/backups
install -m 0755 "${ROOT_DIR}/collector/xray_monitor.py" /opt/xray-monitor/xray_monitor.py
install -m 0644 "${ROOT_DIR}/collector/xray-monitor.service" /etc/systemd/system/xray-monitor.service
install -m 0644 "${ROOT_DIR}/collector/xray-logrotate" /etc/logrotate.d/xray-monitor

if [[ -z "${MONITOR_TOKEN:-}" && -f "${ENV_FILE}" ]]; then
  MONITOR_TOKEN="$(sed -n 's/^XRAY_MONITOR_TOKEN=//p' "${ENV_FILE}" | head -n 1)"
fi
MONITOR_TOKEN="${MONITOR_TOKEN:-$(openssl rand -hex 32)}"
if [[ ! "${MONITOR_TOKEN}" =~ ^[A-Za-z0-9._~-]{20,}$ ]]; then
  echo "MONITOR_TOKEN must be at least 20 URL-safe characters." >&2
  exit 1
fi

umask 077
cat >"${ENV_FILE}" <<EOF
XRAY_MONITOR_TOKEN=${MONITOR_TOKEN}
XRAY_MONITOR_LISTEN=127.0.0.1
XRAY_MONITOR_PORT=8787
XRAY_MONITOR_DB=/var/lib/xray-monitor/monitor.db
XRAY_ACCESS_LOG=${XRAY_ACCESS_LOG}
XRAY_API_SERVER=127.0.0.1:${XRAY_API_PORT}
XRAY_CLI=${XRAY_BIN}
XRAY_MAIN_CONFIG=${XRAY_CONFIG}
XRAY_CONFIG_DIR=${XRAY_CONF_DIR}
XRAY_CF_RANGES=/etc/xray-monitor.cloudflare-ranges
XRAY_MONITOR_HOST=${XRAY_PUBLIC_HOST}
XRAY_MONITOR_BACKUPS=/var/lib/xray-monitor/backups
XRAY_MONITOR_INTERFACE=${NETWORK_INTERFACE}
XRAY_MONITOR_MONTHLY_BYTES=${XRAY_MONITOR_MONTHLY_BYTES}
XRAY_MONITOR_PERIOD_DAYS=${XRAY_MONITOR_PERIOD_DAYS}
XRAY_MONITOR_RESET_ANCHOR=${XRAY_MONITOR_RESET_ANCHOR}
XRAY_MONITOR_STATIC_META=/etc/xray-monitor-static-meta.json
EOF
chmod 0600 "${ENV_FILE}"

if [[ ! -f /etc/xray-monitor-static-meta.json ]]; then
  printf '{}\n' >/etc/xray-monitor-static-meta.json
  chmod 0644 /etc/xray-monitor-static-meta.json
fi

cf_ranges_tmp="$(mktemp)"
trap 'rm -f "${cf_ranges_tmp}"' EXIT
curl -fsSL https://www.cloudflare.com/ips-v4 >"${cf_ranges_tmp}"
curl -fsSL https://www.cloudflare.com/ips-v6 >>"${cf_ranges_tmp}"
install -m 0644 "${cf_ranges_tmp}" /etc/xray-monitor.cloudflare-ranges

install -d -m 0755 "${CADDY_SITES_DIR}"
caddy_site="${CADDY_SITES_DIR}/xray-monitor-api.conf"
caddy_backup=""
if [[ -f "${caddy_site}" ]]; then
  caddy_backup="${caddy_site}.before-${timestamp}"
  cp -a "${caddy_site}" "${caddy_backup}"
fi
cat >"${caddy_site}" <<EOF
${MONITOR_API_DOMAIN} {
    handle /v1/* {
        reverse_proxy 127.0.0.1:8787
    }
    respond 404
}
EOF
chmod 0644 "${caddy_site}"

if ! caddy validate --config /etc/caddy/Caddyfile; then
  if [[ -n "${caddy_backup}" ]]; then
    cp -a "${caddy_backup}" "${caddy_site}"
  else
    rm -f "${caddy_site}"
  fi
  cp -a "${xray_backup}" "${XRAY_CONFIG}"
  echo "Caddy validation failed; Xray and Caddy files were restored." >&2
  exit 1
fi

systemctl daemon-reload
if ! systemctl restart xray; then
  cp -a "${xray_backup}" "${XRAY_CONFIG}"
  if [[ -n "${caddy_backup}" ]]; then
    cp -a "${caddy_backup}" "${caddy_site}"
  else
    rm -f "${caddy_site}"
  fi
  systemctl restart xray || true
  echo "Xray restart failed; the original config was restored." >&2
  exit 1
fi
systemctl enable --now xray-monitor
if ! systemctl reload caddy; then
  if [[ -n "${caddy_backup}" ]]; then
    cp -a "${caddy_backup}" "${caddy_site}"
  else
    rm -f "${caddy_site}"
  fi
  systemctl reload caddy || true
  echo "Caddy reload failed; the previous Caddy site was restored." >&2
  exit 1
fi

for _ in 1 2 3 4 5; do
  if curl -fsS \
    -H "Authorization: Bearer ${MONITOR_TOKEN}" \
    "http://127.0.0.1:8787/v1/snapshot" >/dev/null; then
    break
  fi
  sleep 1
done

if ! curl -fsS \
  -H "Authorization: Bearer ${MONITOR_TOKEN}" \
  "http://127.0.0.1:8787/v1/snapshot" >/dev/null; then
  echo "Collector did not become healthy. Check: journalctl -u xray-monitor" >&2
  exit 1
fi

cat <<EOF

Collector installed successfully.

MONITOR_ENDPOINT=https://${MONITOR_API_DOMAIN}/v1/snapshot
MONITOR_TOKEN=${MONITOR_TOKEN}

Save these two values now. The token is stored on the server in ${ENV_FILE}.
EOF
