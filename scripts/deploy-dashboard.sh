#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

for command in node npm openssl; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    echo "Missing required command: ${command}" >&2
    exit 1
  fi
done

node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
if (( node_major < 22 )); then
  echo "Node.js 22 or newer is required." >&2
  exit 1
fi

if [[ -z "${MONITOR_ENDPOINT:-}" ]]; then
  read -r -p "Collector endpoint (https://.../v1/snapshot): " MONITOR_ENDPOINT
fi
if [[ "${MONITOR_ENDPOINT}" != https://*/v1/snapshot ]]; then
  echo "MONITOR_ENDPOINT must be an HTTPS URL ending in /v1/snapshot." >&2
  exit 1
fi

if [[ -z "${MONITOR_TOKEN:-}" ]]; then
  read -r -s -p "Collector token: " MONITOR_TOKEN
  echo
fi
if (( ${#MONITOR_TOKEN} < 20 )); then
  echo "The collector token must contain at least 20 characters." >&2
  exit 1
fi

ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
  read -r -s -p "Dashboard password: " ADMIN_PASSWORD
  echo
  read -r -s -p "Repeat dashboard password: " password_repeat
  echo
  if [[ "${ADMIN_PASSWORD}" != "${password_repeat}" ]]; then
    echo "The two passwords do not match." >&2
    exit 1
  fi
fi

if (( ${#ADMIN_PASSWORD} < 12 )); then
  echo "Use a dashboard password with at least 12 characters." >&2
  exit 1
fi

SESSION_SECRET="${SESSION_SECRET:-$(openssl rand -hex 32)}"

npm ci

if ! npx wrangler whoami >/dev/null 2>&1; then
  echo "Cloudflare login is required. A browser window will open."
  npx wrangler login
fi

npm run build

secrets_file="$(mktemp)"
trap 'rm -f "${secrets_file}"' EXIT
chmod 0600 "${secrets_file}"

MONITOR_ENDPOINT="${MONITOR_ENDPOINT}" \
MONITOR_TOKEN="${MONITOR_TOKEN}" \
ADMIN_USERNAME="${ADMIN_USERNAME}" \
ADMIN_PASSWORD="${ADMIN_PASSWORD}" \
SESSION_SECRET="${SESSION_SECRET}" \
node <<'NODE' >"${secrets_file}"
const names = [
  "MONITOR_ENDPOINT",
  "MONITOR_TOKEN",
  "ADMIN_USERNAME",
  "ADMIN_PASSWORD",
  "SESSION_SECRET",
];
const secrets = Object.fromEntries(names.map((name) => [name, process.env[name]]));
process.stdout.write(JSON.stringify(secrets));
NODE

npx wrangler deploy --secrets-file "${secrets_file}"

echo
echo "Dashboard deployed. Add a Cloudflare Workers custom domain if desired."
