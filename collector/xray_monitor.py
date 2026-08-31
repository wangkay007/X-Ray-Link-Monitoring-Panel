#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import binascii
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import quote as url_quote, parse_qs, urlparse
except ImportError:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from urllib import quote as url_quote
    from urlparse import parse_qs, urlparse

try:
    text_type = unicode
except NameError:
    text_type = str

DB_PATH = os.environ.get("XRAY_MONITOR_DB", "/var/lib/xray-monitor/monitor.db")
LOG_PATH = os.environ.get("XRAY_ACCESS_LOG", "/var/log/xray/access.log")
TOKEN = os.environ.get("XRAY_MONITOR_TOKEN", "")
LISTEN = os.environ.get("XRAY_MONITOR_LISTEN", "127.0.0.1")
PORT = int(os.environ.get("XRAY_MONITOR_PORT", "8787"))
XRAY = os.environ.get("XRAY_CLI", "/usr/local/bin/xray")
API_SERVER = os.environ.get("XRAY_API_SERVER", "127.0.0.1:47495")
XRAY_MAIN_CONFIG = os.environ.get("XRAY_MAIN_CONFIG", "/etc/xray/config.json")
XRAY_CONFIG_DIR = os.environ.get("XRAY_CONFIG_DIR", "/etc/xray/conf")
CF_RANGES_PATH = os.environ.get("XRAY_CF_RANGES", "/etc/xray-monitor.cloudflare-ranges")
STATIC_META_PATH = os.environ.get("XRAY_MONITOR_STATIC_META", "/etc/xray-monitor-static-meta.json")
SERVER_HOST = os.environ.get("XRAY_MONITOR_HOST", "127.0.0.1")
NET_DEVICE = os.environ.get("XRAY_MONITOR_INTERFACE", "eth0")
SERVER_LIMIT_BYTES = int(os.environ.get("XRAY_MONITOR_MONTHLY_BYTES", str(500 * 1024 * 1024 * 1024)))
SERVER_PERIOD_DAYS = int(os.environ.get("XRAY_MONITOR_PERIOD_DAYS", "30"))
SERVER_RESET_ANCHOR = os.environ.get("XRAY_MONITOR_RESET_ANCHOR", "1970-01-01")
BACKUP_DIR = os.environ.get("XRAY_MONITOR_BACKUPS", "/var/lib/xray-monitor/backups")
XRAY_ERROR_LOG = os.environ.get("XRAY_ERROR_LOG", "/var/log/xray/error.log")
WEBSITE_RETENTION_DAYS = max(1, int(os.environ.get("XRAY_WEBSITE_RETENTION_DAYS", "30")))

PROTOCOLS = {
    "vless-reality": {"label": "VLESS Reality", "alias": "reality", "kind": "reality"},
    "vmess-tcp": {"label": "VMess TCP", "alias": "tcp", "kind": "direct"},
    "vmess-mkcp": {"label": "VMess mKCP", "alias": "kcp", "kind": "direct"},
    "vmess-ws-tls": {"label": "VMess WS TLS", "alias": "ws", "kind": "tls"},
    "vmess-grpc-tls": {"label": "VMess gRPC TLS", "alias": "grpc", "kind": "tls"},
    "vless-ws-tls": {"label": "VLESS WS TLS", "alias": "vws", "kind": "tls"},
    "vless-grpc-tls": {"label": "VLESS gRPC TLS", "alias": "vgrpc", "kind": "tls"},
    "vless-xhttp-tls": {"label": "VLESS XHTTP TLS", "alias": "xhttp", "kind": "tls"},
    "trojan-ws-tls": {"label": "Trojan WS TLS", "alias": "tws", "kind": "tls"},
    "trojan-grpc-tls": {"label": "Trojan gRPC TLS", "alias": "tgrpc", "kind": "tls"},
    "shadowsocks": {"label": "Shadowsocks", "alias": "ss", "kind": "shadowsocks"},
    "socks": {"label": "Socks", "alias": "socks", "kind": "socks"},
}

SERVICE_ACTIONS = {
    "start": ["start"], "stop": ["stop"], "restart": ["restart"],
    "test": ["test"], "fix-all": ["fix-all"],
    "fix-config": ["fix-config.json"], "fix-caddy": ["fix-caddyfile"],
    "update-core": ["update", "core"], "update-script": ["update", "sh"],
    "update-data": ["update", "dat"], "update-caddy": ["update", "caddy"],
}

if SERVER_PERIOD_DAYS < 1:
    raise ValueError("XRAY_MONITOR_PERIOD_DAYS must be at least 1")
try:
    SERVER_RESET_ANCHOR_DATE = datetime.datetime.strptime(SERVER_RESET_ANCHOR, "%Y-%m-%d")
except ValueError:
    raise ValueError("XRAY_MONITOR_RESET_ANCHOR must use YYYY-MM-DD")

def load_static_meta():
    try:
        payload = json.load(open(STATIC_META_PATH, "r"))
    except Exception:
        return {}
    result = {}
    for tag, values in payload.items():
        if isinstance(values, list) and len(values) == 3:
            result[tag] = tuple(values)
    return result


STATIC_META = load_static_meta()

ACCESS_RE = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\.\d+ from (\[[^\]]+\]|[^: ]+):\d+ accepted (tcp|udp):(.+):(\d+) \[([^ ]+) (?:->|>>) [^\]]+\](?: email: ([^ ]+))?")
STAT_RE = re.compile(r'^inbound>>>(.+)>>>traffic>>>(uplink|downlink)$')
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
MUTATION_LOCK = threading.Lock()


def now():
    return int(time.time())


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = lambda cursor, row: dict((str(column[0]), row[index]) for index, column in enumerate(cursor.description))
    return conn


def init_db():
    parent = os.path.dirname(DB_PATH)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    if not os.path.isdir(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS counters (
      tag TEXT PRIMARY KEY, last_up INTEGER NOT NULL DEFAULT 0,
      last_down INTEGER NOT NULL DEFAULT 0, total_up INTEGER NOT NULL DEFAULT 0,
      total_down INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS ip_usage (
      tag TEXT NOT NULL, ip TEXT NOT NULL, first_seen INTEGER NOT NULL,
      last_seen INTEGER NOT NULL, connections INTEGER NOT NULL DEFAULT 1,
      PRIMARY KEY (tag, ip)
    );
    CREATE TABLE IF NOT EXISTS samples (
      bucket INTEGER NOT NULL, tag TEXT NOT NULL, up_bytes INTEGER NOT NULL DEFAULT 0,
      down_bytes INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bucket, tag)
    );
    CREATE INDEX IF NOT EXISTS ip_last_seen_idx ON ip_usage(last_seen DESC);
    CREATE TABLE IF NOT EXISTS quotas (
      tag TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
      limit_bytes INTEGER NOT NULL DEFAULT 0, reset_day INTEGER NOT NULL DEFAULT 1,
      period_start INTEGER NOT NULL DEFAULT 0, baseline_up INTEGER NOT NULL DEFAULT 0,
      baseline_down INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL DEFAULT 0, last_error TEXT
    );
    CREATE TABLE IF NOT EXISTS expirations (
      tag TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
      expires_at INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL DEFAULT 0, last_error TEXT
    );
    CREATE TABLE IF NOT EXISTS ip_controls (
      ip TEXT PRIMARY KEY, device_label TEXT NOT NULL DEFAULT '',
      blocked INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS uuid_controls (
      tag TEXT NOT NULL, uuid TEXT NOT NULL, device_label TEXT NOT NULL DEFAULT '',
      disabled INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0,
      last_error TEXT, PRIMARY KEY (tag, uuid)
    );
    CREATE TABLE IF NOT EXISTS link_meta (
      tag TEXT PRIMARY KEY, name TEXT NOT NULL, managed INTEGER NOT NULL DEFAULT 0,
      share_uri TEXT, created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS server_traffic (
      month TEXT PRIMARY KEY, last_rx INTEGER NOT NULL DEFAULT 0,
      last_tx INTEGER NOT NULL DEFAULT 0, total_rx INTEGER NOT NULL DEFAULT 0,
      total_tx INTEGER NOT NULL DEFAULT 0, measured_since INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS command_audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
      target TEXT NOT NULL DEFAULT '', success INTEGER NOT NULL DEFAULT 0,
      output TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS command_audit_created_idx ON command_audit(created_at DESC);
    CREATE TABLE IF NOT EXISTS website_usage (
      bucket INTEGER NOT NULL, tag TEXT NOT NULL, ip TEXT NOT NULL,
      target TEXT NOT NULL, port INTEGER NOT NULL, network TEXT NOT NULL,
      email TEXT NOT NULL DEFAULT '', connections INTEGER NOT NULL DEFAULT 1,
      first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
      PRIMARY KEY (bucket, tag, ip, target, port, network, email)
    );
    CREATE INDEX IF NOT EXISTS website_usage_last_idx ON website_usage(last_seen DESC);
    CREATE INDEX IF NOT EXISTS website_usage_target_idx ON website_usage(target, last_seen DESC);
    CREATE INDEX IF NOT EXISTS website_usage_tag_idx ON website_usage(tag, last_seen DESC);
    CREATE TABLE IF NOT EXISTS log_cursor (
      path TEXT PRIMARY KEY, inode INTEGER NOT NULL DEFAULT 0,
      offset INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0
    );
    """)
    migrate_server_traffic(conn)
    conn.commit()
    conn.close()


def load_cf_ranges():
    result = []
    try:
        values = [line.strip() for line in open(CF_RANGES_PATH, "r") if line.strip()]
    except IOError:
        values = []
    for value in values:
        try:
            address, prefix = value.split("/", 1)
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            bits = 128 if family == socket.AF_INET6 else 32
            number = int(binascii.hexlify(socket.inet_pton(family, address)), 16)
            prefix = int(prefix)
            mask = ((1 << bits) - 1) ^ ((1 << (bits - prefix)) - 1)
            result.append((family, number & mask, mask))
        except Exception:
            continue
    return result


CF_RANGES = load_cf_ranges()


def is_cloudflare_ip(address):
    try:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        number = int(binascii.hexlify(socket.inet_pton(family, address)), 16)
    except Exception:
        return False
    return any(family == item[0] and number & item[2] == item[1] for item in CF_RANGES)


def valid_ip(address):
    try:
        socket.inet_pton(socket.AF_INET6 if ":" in address else socket.AF_INET, address)
        return True
    except Exception:
        return False


def read_json(path):
    with open(path, "r") as handle:
        return json.load(handle)


def device_email(client_id):
    return "device-%s@monitor.local" % hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:20]


def device_code(client_id):
    return "DEV-" + client_id.replace("-", "")[:8].upper()


def protocol_id(protocol, network, security):
    if protocol == "vless" and security == "reality": return "vless-reality"
    if protocol == "vmess" and network == "tcp": return "vmess-tcp"
    if protocol == "vmess" and network == "kcp": return "vmess-mkcp"
    if protocol == "vmess" and network == "ws": return "vmess-ws-tls"
    if protocol == "vmess" and network == "grpc": return "vmess-grpc-tls"
    if protocol == "vless" and network == "ws": return "vless-ws-tls"
    if protocol == "vless" and network == "grpc": return "vless-grpc-tls"
    if protocol == "vless" and network in ("xhttp", "splithttp"): return "vless-xhttp-tls"
    if protocol == "trojan" and network == "ws": return "trojan-ws-tls"
    if protocol == "trojan" and network == "grpc": return "trojan-grpc-tls"
    if protocol == "shadowsocks": return "shadowsocks"
    if protocol == "socks": return "socks"
    return None


def discover_links():
    conn = db()
    meta_rows = {row["tag"]: row for row in conn.execute("SELECT * FROM link_meta")}
    uuid_rows = {(row["tag"], row["uuid"]): row for row in conn.execute("SELECT * FROM uuid_controls")}
    conn.close()
    result = {}
    for path in sorted(glob.glob(os.path.join(XRAY_CONFIG_DIR, "*.json"))):
        try:
            payload = read_json(path)
        except Exception:
            continue
        for inbound in payload.get("inbounds", []):
            tag = inbound.get("tag")
            protocol = inbound.get("protocol", "unknown")
            if not tag:
                continue
            stream = inbound.get("streamSettings", {})
            network = stream.get("network", "tcp")
            security = stream.get("security")
            protocol_key = protocol_id(protocol, network, security)
            if not protocol_key:
                continue
            protocol_label = PROTOCOLS[protocol_key]["label"]
            port = int(inbound.get("port", 0) or 0)
            static = STATIC_META.get(tag)
            metadata = meta_rows.get(tag)
            name = metadata["name"] if metadata else (static[0] if static else tag.replace(".json", ""))
            endpoint = SERVER_HOST + ":" + str(port)
            transport = "direct"
            if static:
                endpoint, transport = static[1], static[2]
            elif PROTOCOLS[protocol_key]["kind"] == "tls":
                headers = stream.get("wsSettings", {}).get("headers", {})
                grpc = stream.get("grpcSettings", {})
                xhttp = stream.get("xhttpSettings", stream.get("splithttpSettings", {}))
                xhttp_host = xhttp.get("host")
                if isinstance(xhttp_host, list): xhttp_host = xhttp_host[0] if xhttp_host else None
                endpoint = headers.get("Host") or headers.get("host") or stream.get("grpc_host") or xhttp_host or endpoint
                transport = "cdn"
            devices = []
            for client in inbound.get("settings", {}).get("clients", []):
                client_id = client.get("id")
                if not client_id:
                    continue
                control = uuid_rows.get((tag, client_id), {})
                devices.append({
                    "uuid": client_id,
                    "code": device_code(client_id),
                    "deviceLabel": control.get("device_label") or "",
                    "disabled": bool(control.get("disabled")),
                    "lastError": control.get("last_error"),
                })
            ws = stream.get("wsSettings", {})
            grpc = stream.get("grpcSettings", {})
            xhttp = stream.get("xhttpSettings", stream.get("splithttpSettings", {}))
            reality = stream.get("realitySettings", {})
            host = ws.get("headers", {}).get("Host") or ws.get("headers", {}).get("host") or stream.get("grpc_host") or xhttp.get("host") or ""
            if isinstance(host, list): host = host[0] if host else ""
            path_value = ws.get("path") or grpc.get("serviceName") or xhttp.get("path") or ""
            settings = inbound.get("settings", {})
            accounts = settings.get("accounts", [])
            result[tag] = {
                "id": tag, "name": name, "protocol": protocol_label,
                "endpoint": endpoint, "transport": transport, "port": port,
                "managed": True,
                "shareUri": metadata.get("share_uri") if metadata else None,
                "devices": devices,
                "config": {
                    "protocolId": protocol_key, "host": host, "path": path_value,
                    "sni": (reality.get("serverNames") or [""])[0],
                    "headerType": stream.get("tcpSettings", {}).get("header", {}).get("type") or stream.get("kcpSettings", {}).get("header", {}).get("type") or "none",
                    "method": settings.get("method") or "",
                    "username": accounts[0].get("user", "") if accounts else "",
                },
                "configPath": path,
            }
    return result


def purge_edge_ips():
    conn = db()
    for row in conn.execute("SELECT tag,ip FROM ip_usage"):
        if is_cloudflare_ip(row["ip"]):
            conn.execute("DELETE FROM ip_usage WHERE tag=? AND ip=?", (row["tag"], row["ip"]))
    conn.commit()
    conn.close()


def query_stats(links):
    try:
        raw = subprocess.check_output([XRAY, "api", "statsquery", "--server=" + API_SERVER], stderr=subprocess.STDOUT)
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    values = {}
    for item in payload.get("stat", []):
        match = STAT_RE.match(item.get("name", ""))
        if not match or match.group(1) not in links:
            continue
        tag, direction = match.groups()
        values.setdefault(tag, {"uplink": 0, "downlink": 0})
        values[tag][direction] = int(item.get("value", 0) or 0)
    return values


def read_network_counters():
    try:
        for line in open("/proc/net/dev", "r"):
            if ":" not in line:
                continue
            name, values = line.split(":", 1)
            if name.strip() == NET_DEVICE:
                fields = values.split()
                return int(fields[0]), int(fields[8])
    except Exception:
        pass
    return None


def server_period_bounds(stamp):
    current = datetime.datetime.fromtimestamp(stamp)
    elapsed_days = (current.date() - SERVER_RESET_ANCHOR_DATE.date()).days
    period_index = elapsed_days // SERVER_PERIOD_DAYS
    start = SERVER_RESET_ANCHOR_DATE + datetime.timedelta(days=period_index * SERVER_PERIOD_DAYS)
    next_start = start + datetime.timedelta(days=SERVER_PERIOD_DAYS)
    return int(time.mktime(start.timetuple())), int(time.mktime(next_start.timetuple()))


def server_period_key(stamp):
    period_start, unused_next = server_period_bounds(stamp)
    return time.strftime("%Y-%m-%d", time.localtime(period_start))


def migrate_server_traffic(conn):
    """Fold legacy calendar-month counters into the active billing period."""
    stamp = now()
    period_start, next_reset = server_period_bounds(stamp)
    period_key = server_period_key(stamp)
    if conn.execute("SELECT 1 FROM server_traffic WHERE month=?", (period_key,)).fetchone():
        return
    legacy = list(conn.execute(
        "SELECT * FROM server_traffic WHERE length(month)=7 AND updated_at>=? AND measured_since<? ORDER BY updated_at",
        (period_start, next_reset),
    ))
    if not legacy:
        return
    latest = legacy[-1]
    conn.execute(
        "INSERT INTO server_traffic(month,last_rx,last_tx,total_rx,total_tx,measured_since,updated_at) VALUES(?,?,?,?,?,?,?)",
        (period_key, latest["last_rx"], latest["last_tx"],
         sum(int(row["total_rx"]) for row in legacy), sum(int(row["total_tx"]) for row in legacy),
         min(int(row["measured_since"]) for row in legacy), max(int(row["updated_at"]) for row in legacy)),
    )
    conn.executemany("DELETE FROM server_traffic WHERE month=?", [(row["month"],) for row in legacy])


def update_server_traffic():
    current = read_network_counters()
    if current is None:
        return
    stamp = now()
    month = server_period_key(stamp)
    conn = db()
    row = conn.execute("SELECT * FROM server_traffic WHERE month=?", (month,)).fetchone()
    if row:
        delta_rx = current[0] - row["last_rx"] if current[0] >= row["last_rx"] else current[0]
        delta_tx = current[1] - row["last_tx"] if current[1] >= row["last_tx"] else current[1]
        conn.execute("UPDATE server_traffic SET last_rx=?,last_tx=?,total_rx=total_rx+?,total_tx=total_tx+?,updated_at=? WHERE month=?",
                     (current[0], current[1], max(0, delta_rx), max(0, delta_tx), stamp, month))
    else:
        conn.execute("INSERT INTO server_traffic(month,last_rx,last_tx,total_rx,total_tx,measured_since,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (month, current[0], current[1], 0, 0, stamp, stamp))
    conn.execute("DELETE FROM server_traffic WHERE updated_at < ?", (stamp - 370 * 86400,))
    conn.commit()
    conn.close()


def update_stats():
    links = discover_links()
    values = query_stats(links)
    update_server_traffic()
    if values is None:
        return
    conn = db()
    stamp = now()
    bucket = stamp - stamp % 300
    for tag in links:
        current = values.get(tag, {"uplink": 0, "downlink": 0})
        row = conn.execute("SELECT * FROM counters WHERE tag=?", (tag,)).fetchone()
        if row is None:
            delta_up, delta_down = current["uplink"], current["downlink"]
            total_up, total_down = delta_up, delta_down
        else:
            delta_up = current["uplink"] - row["last_up"] if current["uplink"] >= row["last_up"] else current["uplink"]
            delta_down = current["downlink"] - row["last_down"] if current["downlink"] >= row["last_down"] else current["downlink"]
            total_up = row["total_up"] + max(0, delta_up)
            total_down = row["total_down"] + max(0, delta_down)
        conn.execute("INSERT OR REPLACE INTO counters(tag,last_up,last_down,total_up,total_down,updated_at) VALUES(?,?,?,?,?,?)",
                     (tag, current["uplink"], current["downlink"], total_up, total_down, stamp))
        conn.execute("INSERT OR IGNORE INTO samples(bucket,tag,up_bytes,down_bytes) VALUES(?,?,0,0)", (bucket, tag))
        conn.execute("UPDATE samples SET up_bytes=up_bytes+?,down_bytes=down_bytes+? WHERE bucket=? AND tag=?",
                     (max(0, delta_up), max(0, delta_down), bucket, tag))
    conn.execute("DELETE FROM samples WHERE bucket < ?", (stamp - 30 * 86400,))
    conn.commit()
    conn.close()
    enforce_quotas()
    enforce_expirations()
    apply_uuid_controls()


def quota_period(reset_day, stamp=None):
    current = datetime.datetime.fromtimestamp(stamp or now())
    if current.day < reset_day:
        year, month = (current.year - 1, 12) if current.month == 1 else (current.year, current.month - 1)
    else:
        year, month = current.year, current.month
    start = datetime.datetime(year, month, reset_day)
    next_start = datetime.datetime(year + 1, 1, reset_day) if month == 12 else datetime.datetime(year, month + 1, reset_day)
    return int(time.mktime(start.timetuple())), int(time.mktime(next_start.timetuple()))


def set_inbound_enabled(tag, enabled):
    links = discover_links()
    if tag not in links:
        return "unknown link"
    try:
        if enabled:
            subprocess.check_output([XRAY, "api", "adi", "--server=" + API_SERVER, links[tag]["configPath"]], stderr=subprocess.STDOUT)
            apply_uuid_controls(tag)
        else:
            subprocess.check_output([XRAY, "api", "rmi", "--server=" + API_SERVER, tag], stderr=subprocess.STDOUT)
        return None
    except subprocess.CalledProcessError as error:
        message = error.output.decode("utf-8", "replace") if error.output else str(error)
        lowered = message.lower()
        if (not enabled and ("not found" in lowered or "failed to remove" in lowered)) or (enabled and ("already exists" in lowered or "address already in use" in lowered)):
            return None
        return message[-400:]
    except Exception as error:
        return str(error)


def expiration_blocks(conn, tag, stamp):
    row = conn.execute("SELECT enabled,expires_at FROM expirations WHERE tag=?", (tag,)).fetchone()
    return bool(row and row["enabled"] and int(row["expires_at"]) > 0 and stamp >= int(row["expires_at"]))


def quota_blocks(conn, tag, stamp):
    row = conn.execute("SELECT q.*,c.total_up,c.total_down FROM quotas q LEFT JOIN counters c ON c.tag=q.tag WHERE q.tag=?", (tag,)).fetchone()
    if not row or not row["enabled"] or int(row["limit_bytes"]) <= 0:
        return False
    period_start, unused_next = quota_period(int(row["reset_day"]), stamp)
    if int(row["period_start"]) != period_start:
        return False
    used = max(0, int(row.get("total_up") or 0) - int(row["baseline_up"])) + max(0, int(row.get("total_down") or 0) - int(row["baseline_down"]))
    return used >= int(row["limit_bytes"])


def enforce_quotas():
    conn = db()
    stamp = now()
    rows = list(conn.execute("SELECT q.*,c.total_up,c.total_down FROM quotas q LEFT JOIN counters c ON c.tag=q.tag"))
    for row in rows:
        total_up, total_down = int(row.get("total_up") or 0), int(row.get("total_down") or 0)
        period_start, unused_next = quota_period(int(row["reset_day"]), stamp)
        baseline_up, baseline_down = int(row["baseline_up"]), int(row["baseline_down"])
        was_disabled = bool(row["disabled"])
        if int(row["period_start"]) != period_start:
            baseline_up, baseline_down = total_up, total_down
        used = max(0, total_up - baseline_up) + max(0, total_down - baseline_down)
        should_disable = bool(row["enabled"]) and int(row["limit_bytes"]) > 0 and used >= int(row["limit_bytes"])
        error = None
        if should_disable:
            error = set_inbound_enabled(row["tag"], False)
            disabled = error is None
        elif was_disabled:
            if expiration_blocks(conn, row["tag"], stamp):
                disabled = False
            else:
                error = set_inbound_enabled(row["tag"], True)
                disabled = error is not None
        else:
            disabled = False
        conn.execute("UPDATE quotas SET period_start=?,baseline_up=?,baseline_down=?,disabled=?,updated_at=?,last_error=? WHERE tag=?",
                     (period_start, baseline_up, baseline_down, 1 if disabled else 0, stamp, error, row["tag"]))
    conn.commit()
    conn.close()


def enforce_expirations():
    conn = db()
    stamp = now()
    rows = list(conn.execute("SELECT * FROM expirations"))
    for row in rows:
        should_disable = bool(row["enabled"]) and int(row["expires_at"]) > 0 and stamp >= int(row["expires_at"])
        was_disabled = bool(row["disabled"])
        error = None
        if should_disable:
            error = set_inbound_enabled(row["tag"], False)
            disabled = error is None
        elif was_disabled:
            if quota_blocks(conn, row["tag"], stamp):
                disabled = False
            else:
                error = set_inbound_enabled(row["tag"], True)
                disabled = error is not None
        else:
            disabled = False
        conn.execute("UPDATE expirations SET disabled=?,updated_at=?,last_error=? WHERE tag=?",
                     (1 if disabled else 0, stamp, error, row["tag"]))
    conn.commit()
    conn.close()


def save_quota(payload):
    tag = payload.get("tag")
    if tag not in discover_links():
        raise ValueError("未知链接")
    enabled = bool(payload.get("enabled"))
    try:
        limit_bytes, reset_day = int(payload.get("limitBytes", 0)), int(payload.get("resetDay", 1))
    except Exception:
        raise ValueError("额度格式不正确")
    if limit_bytes < 0:
        raise ValueError("额度不能小于 0")
    if reset_day < 1 or reset_day > 28:
        raise ValueError("重置日必须在 1 到 28 日之间")
    conn = db()
    counter = conn.execute("SELECT * FROM counters WHERE tag=?", (tag,)).fetchone() or {}
    existing = conn.execute("SELECT * FROM quotas WHERE tag=?", (tag,)).fetchone()
    period_start, unused_next = quota_period(reset_day)
    if existing and int(existing["period_start"]) == period_start:
        baseline_up, baseline_down, disabled = int(existing["baseline_up"]), int(existing["baseline_down"]), int(existing["disabled"])
    else:
        baseline_up, baseline_down, disabled = int(counter.get("total_up") or 0), int(counter.get("total_down") or 0), 0
    conn.execute("INSERT OR REPLACE INTO quotas(tag,enabled,limit_bytes,reset_day,period_start,baseline_up,baseline_down,disabled,updated_at,last_error) VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                 (tag, 1 if enabled else 0, limit_bytes, reset_day, period_start, baseline_up, baseline_down, disabled, now()))
    conn.commit()
    conn.close()
    enforce_quotas()
    enforce_expirations()


def save_expiration(payload):
    tag = payload.get("tag")
    if tag not in discover_links():
        raise ValueError("未知链接")
    enabled = bool(payload.get("enabled"))
    try:
        expires_at = int(payload.get("expiresAt", 0))
    except Exception:
        raise ValueError("到期时间格式不正确")
    if enabled and expires_at <= 0:
        raise ValueError("启用到期限制时必须设置到期时间")
    conn = db()
    existing = conn.execute("SELECT disabled FROM expirations WHERE tag=?", (tag,)).fetchone()
    disabled = int(existing["disabled"]) if existing else 0
    conn.execute("INSERT OR REPLACE INTO expirations(tag,enabled,expires_at,disabled,updated_at,last_error) VALUES(?,?,?,?,?,NULL)",
                 (tag, 1 if enabled else 0, expires_at if enabled else 0, disabled, now()))
    conn.commit()
    conn.close()
    enforce_quotas()
    enforce_expirations()


def block_rule_tag(tag):
    return "monitorBlock" + hashlib.sha1(tag.encode("utf-8")).hexdigest()[:12]


def apply_block_rules():
    conn = db()
    addresses = [row["ip"] for row in conn.execute("SELECT ip FROM ip_controls WHERE blocked=1 ORDER BY ip")]
    conn.close()
    # Xray's `sib -reset` reads stdin when no address is provided. A TEST-NET
    # address keeps the rule valid while matching no legitimate client.
    if not addresses:
        addresses = ["192.0.2.1"]
    errors = []
    for tag in discover_links():
        command = [XRAY, "api", "sib", "--server=" + API_SERVER, "-outbound=block", "-inbound=" + tag,
                   "-ruletag=" + block_rule_tag(tag), "-reset"] + addresses
        try:
            subprocess.check_output(command, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as error:
            output = error.output.decode("utf-8", "replace") if error.output else str(error)
            errors.append(tag + ": " + output[-180:])
    if errors:
        raise RuntimeError("; ".join(errors))


def save_ip_control(payload):
    address = (payload.get("ip") or "").strip()
    if not valid_ip(address):
        raise ValueError("IP 地址不正确")
    label = (payload.get("deviceLabel") or "").strip()
    if len(label) > 40:
        raise ValueError("设备备注最多 40 个字符")
    blocked = bool(payload.get("blocked"))
    scope = payload.get("scope") or "ip"
    conn = db()
    conn.execute("INSERT OR IGNORE INTO ip_controls(ip,device_label,blocked,updated_at) VALUES(?,?,0,?)", (address, label, now()))
    conn.execute("UPDATE ip_controls SET device_label=?,updated_at=? WHERE ip=?", (label, now(), address))
    if scope == "device" and label:
        conn.execute("UPDATE ip_controls SET blocked=?,updated_at=? WHERE device_label=?", (1 if blocked else 0, now(), label))
    else:
        conn.execute("UPDATE ip_controls SET blocked=?,updated_at=? WHERE ip=?", (1 if blocked else 0, now(), address))
    conn.commit()
    conn.close()
    apply_block_rules()


def find_uuid_client(tag, client_id):
    link = discover_links().get(tag)
    if not link:
        return None, None
    payload = read_json(link["configPath"])
    for inbound in payload.get("inbounds", []):
        if inbound.get("tag") != tag:
            continue
        for client in inbound.get("settings", {}).get("clients", []):
            if client.get("id") == client_id:
                item = dict(client)
                item["email"] = item.get("email") or device_email(client_id)
                return inbound, item
    return None, None


def set_uuid_disabled(tag, client_id, disabled):
    inbound, client = find_uuid_client(tag, client_id)
    if not client:
        return "unknown UUID"
    email = client["email"]
    try:
        if disabled:
            subprocess.check_output([XRAY, "api", "rmu", "--server=" + API_SERVER, "-tag=" + tag, email], stderr=subprocess.STDOUT)
        else:
            fd, path = tempfile.mkstemp(prefix="monitor-user-", suffix=".json")
            try:
                handle = os.fdopen(fd, "w")
                inbound_for_user = json.loads(json.dumps(inbound))
                inbound_for_user.setdefault("settings", {})["clients"] = [client]
                json.dump({"inbounds": [inbound_for_user]}, handle)
                handle.close()
                subprocess.check_output([XRAY, "api", "adu", "--server=" + API_SERVER, path], stderr=subprocess.STDOUT)
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        return None
    except subprocess.CalledProcessError as error:
        message = error.output.decode("utf-8", "replace") if error.output else str(error)
        lowered = message.lower()
        if disabled and ("not found" in lowered or "failed to remove" in lowered):
            return None
        if not disabled and ("already exists" in lowered or "already registered" in lowered or "not found" in lowered):
            return None
        return message[-300:]
    except Exception as error:
        return str(error)


def apply_uuid_controls(only_tag=None):
    conn = db()
    if only_tag:
        rows = list(conn.execute("SELECT * FROM uuid_controls WHERE disabled=1 AND tag=?", (only_tag,)))
    else:
        rows = list(conn.execute("SELECT * FROM uuid_controls WHERE disabled=1"))
    for row in rows:
        error = set_uuid_disabled(row["tag"], row["uuid"], True)
        conn.execute("UPDATE uuid_controls SET last_error=?,updated_at=? WHERE tag=? AND uuid=?",
                     (error, now(), row["tag"], row["uuid"]))
    conn.commit()
    conn.close()


def save_uuid_control(payload):
    tag = payload.get("tag")
    client_id = (payload.get("uuid") or "").strip().lower()
    inbound, client = find_uuid_client(tag, client_id)
    if not client:
        raise ValueError("该链接中不存在此 UUID")
    label = (payload.get("deviceLabel") or "").strip()
    if len(label) > 40:
        raise ValueError("设备备注最多 40 个字符")
    disabled = bool(payload.get("disabled"))
    conn = db()
    conn.execute("INSERT OR REPLACE INTO uuid_controls(tag,uuid,device_label,disabled,updated_at,last_error) VALUES(?,?,?,?,?,NULL)",
                 (tag, client_id, label, 1 if disabled else 0, now()))
    conn.commit()
    conn.close()
    error = set_uuid_disabled(tag, client_id, disabled)
    conn = db()
    conn.execute("UPDATE uuid_controls SET last_error=?,updated_at=? WHERE tag=? AND uuid=?", (error, now(), tag, client_id))
    conn.commit()
    conn.close()
    if error:
        raise RuntimeError(error)


def record_access(line, conn=None, record_ip=True):
    match = ACCESS_RE.match(line)
    if not match:
        return
    date_text, address, network, target, target_port, tag, email = match.groups()
    if os.path.basename(tag) != tag or not os.path.isfile(os.path.join(XRAY_CONFIG_DIR, tag)):
        return
    address = address.strip("[]")
    if is_cloudflare_ip(address):
        return
    target = target.strip("[]").strip().lower()
    if len(target) > 253 or not (valid_ip(target) or (DOMAIN_RE.match(target) and ".." not in target)):
        return
    try:
        target_port = int(target_port)
    except Exception:
        return
    email = (email or "").strip()[:254]
    try:
        stamp = int(time.mktime(time.strptime(date_text, "%Y/%m/%d %H:%M:%S")))
    except Exception:
        stamp = now()
    owns_connection = conn is None
    if owns_connection: conn = db()
    if record_ip:
        row = conn.execute("SELECT connections FROM ip_usage WHERE tag=? AND ip=?", (tag, address)).fetchone()
        if row:
            conn.execute("UPDATE ip_usage SET last_seen=?,connections=? WHERE tag=? AND ip=?", (stamp, row["connections"] + 1, tag, address))
        else:
            conn.execute("INSERT INTO ip_usage(tag,ip,first_seen,last_seen,connections) VALUES(?,?,?,?,1)", (tag, address, stamp, stamp))
        conn.execute("INSERT OR IGNORE INTO ip_controls(ip,device_label,blocked,updated_at) VALUES(?,'',0,?)", (address, stamp))
    bucket = stamp - stamp % 300
    usage = conn.execute("SELECT connections,first_seen FROM website_usage WHERE bucket=? AND tag=? AND ip=? AND target=? AND port=? AND network=? AND email=?",
                         (bucket, tag, address, target, target_port, network, email)).fetchone()
    if usage:
        conn.execute("UPDATE website_usage SET connections=?,last_seen=? WHERE bucket=? AND tag=? AND ip=? AND target=? AND port=? AND network=? AND email=?",
                     (int(usage["connections"]) + 1, stamp, bucket, tag, address, target, target_port, network, email))
    else:
        conn.execute("INSERT INTO website_usage(bucket,tag,ip,target,port,network,email,connections,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,1,?,?)",
                     (bucket, tag, address, target, target_port, network, email, stamp, stamp))
    if stamp % 300 < 2:
        conn.execute("DELETE FROM website_usage WHERE last_seen<?", (stamp - WEBSITE_RETENTION_DAYS * 86400,))
    if owns_connection:
        conn.commit()
        conn.close()


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{1,180}$")
SS_METHODS = ("aes-128-gcm", "aes-256-gcm", "chacha20-poly1305", "xchacha20-poly1305")


def clean_output(value):
    return ANSI_RE.sub("", value or "").strip()


def audit_command(action, target, success, output):
    try:
        conn = db()
        conn.execute("INSERT INTO command_audit(action,target,success,output,created_at) VALUES(?,?,?,?,?)",
                     (action, target or "", 1 if success else 0, clean_output(output)[-2000:], now()))
        conn.commit()
        conn.close()
    except Exception:
        pass


def run_process(args, timeout=180):
    process = subprocess.Popen(list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.time() + timeout
    while process.poll() is None:
        if time.time() >= deadline:
            process.kill()
            output = process.communicate()[0]
            raise RuntimeError("命令执行超时\n" + clean_output(output.decode("utf-8", "replace"))[-400:])
        time.sleep(0.1)
    output = process.communicate()[0]
    value = clean_output(output.decode("utf-8", "replace"))
    if process.returncode != 0: raise RuntimeError(value[-800:] or "命令执行失败")
    return value


def run_xray(args, timeout=180, audit_action=None, target=""):
    try:
        value = run_process([XRAY] + list(args), timeout)
        if audit_action: audit_command(audit_action, target, True, value)
        return value
    except Exception as error:
        value = clean_output(text_type(error))
        if audit_action: audit_command(audit_action, target, False, value)
        raise RuntimeError(value[-800:] or "Xray 命令执行失败")


def validate_name(value):
    name = (value or "").strip()
    if not name or len(name) > 32:
        raise ValueError("名称需为 1 到 32 个字符")
    return name


def validate_port(value, current_tag=None):
    try: port = int(value)
    except Exception: raise ValueError("端口格式不正确")
    if port < 1024 or port > 65535: raise ValueError("端口需在 1024 到 65535 之间")
    for tag, link in discover_links().items():
        if tag != current_tag and link["port"] == port:
            raise ValueError("该端口已被其他 Xray 链接使用")
    return port


def validate_domain(value, label="域名"):
    domain = (value or "").strip().lower()
    if not domain or not DOMAIN_RE.match(domain) or ".." in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError(label + "格式不正确")
    return domain


def validate_path(value):
    path = (value or "").strip()
    if not PATH_RE.match(path): raise ValueError("路径需以 / 开头，且不能包含空格")
    return path


def validate_secret(value, label="密码"):
    secret = (value or "").strip()
    if len(secret) < 8 or len(secret) > 128 or any(ord(char) < 33 for char in secret):
        raise ValueError(label + "需为 8 到 128 个非空白字符")
    return secret


def generate_uuid():
    value = run_xray(["uuid"]).splitlines()[-1].strip()
    if not UUID_RE.match(value): raise RuntimeError("无法生成 UUID")
    return value


def manager_args(payload):
    protocol_key = (payload.get("protocolId") or "vless-reality").strip()
    if protocol_key not in PROTOCOLS: raise ValueError("不支持该协议")
    definition = PROTOCOLS[protocol_key]
    kind = definition["kind"]
    args = [definition["alias"]]
    if kind == "reality":
        port = validate_port(payload.get("port"))
        client_id = (payload.get("uuid") or "").strip() or generate_uuid()
        if not UUID_RE.match(client_id): raise ValueError("UUID 格式不正确")
        args += [str(port), client_id, validate_domain(payload.get("sni") or "www.cloudflare.com", "Reality SNI")]
    elif kind == "direct":
        port = validate_port(payload.get("port"))
        client_id = (payload.get("uuid") or "").strip() or generate_uuid()
        if not UUID_RE.match(client_id): raise ValueError("UUID 格式不正确")
        header = (payload.get("headerType") or "none").strip().lower()
        if header not in ("none", "http", "srtp", "utp", "wechat-video", "dtls", "wireguard"):
            raise ValueError("伪装类型不受支持")
        args += [str(port), client_id, header]
    elif kind == "tls":
        host = validate_domain(payload.get("host"), "TLS 域名")
        client_id = (payload.get("uuid") or "").strip() or generate_uuid()
        if not UUID_RE.match(client_id): raise ValueError("UUID 或 Trojan 密码格式不正确")
        args += [host, client_id, validate_path(payload.get("path") or ("/" + client_id.replace("-", "")[:16]))]
    elif kind == "shadowsocks":
        method = (payload.get("method") or "aes-256-gcm").strip().lower()
        if method not in SS_METHODS: raise ValueError("Shadowsocks 加密方式不受支持")
        args += [str(validate_port(payload.get("port"))), validate_secret(payload.get("password") or generate_uuid()), method]
    elif kind == "socks":
        username = (payload.get("username") or "").strip()
        if not re.match(r"^[A-Za-z0-9._-]{3,32}$", username): raise ValueError("Socks 用户名格式不正确")
        args += [str(validate_port(payload.get("port"))), username, validate_secret(payload.get("password"))]
    return protocol_key, args


def test_all_configs():
    run_xray(["run", "-test", "-config", XRAY_MAIN_CONFIG, "-confdir", XRAY_CONFIG_DIR])


def atomic_write_json(path, payload):
    fd, temporary = tempfile.mkstemp(prefix=".monitor-", suffix=".json", dir=XRAY_CONFIG_DIR)
    try:
        handle = os.fdopen(fd, "w")
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.close()
        os.rename(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def xray_files():
    return set(os.path.basename(path) for path in glob.glob(os.path.join(XRAY_CONFIG_DIR, "*.json")) if "-link.json" not in path)


def extract_share_uri(tag):
    output = run_xray(["url", tag], timeout=30)
    matches = re.findall(r"(?:vmess|vless|trojan|ss|socks)://[^\s\x1b]+", output)
    return matches[0] if matches else None


def save_link_meta(tag, name, uri=None):
    stamp = now()
    conn = db()
    conn.execute("INSERT OR REPLACE INTO link_meta(tag,name,managed,share_uri,created_at,updated_at) VALUES(?,?,1,?,COALESCE((SELECT created_at FROM link_meta WHERE tag=?),?),?)",
                 (tag, name, uri, tag, stamp, stamp))
    conn.commit()
    conn.close()


def create_link(payload):
    name = validate_name(payload.get("name"))
    protocol_key, args = manager_args(payload)
    before = xray_files()
    run_xray(["add"] + args, audit_action="link.create", target=protocol_key)
    created = sorted(xray_files() - before)
    if len(created) != 1:
        raise RuntimeError("配置已创建，但无法唯一识别新链接；请刷新后检查")
    tag = created[0]
    uri = extract_share_uri(tag)
    save_link_meta(tag, name, uri)
    apply_block_rules()
    return uri


def migrate_tag(old_tag, new_tag):
    if old_tag == new_tag: return
    conn = db()
    for table in ("counters", "ip_usage", "samples", "quotas", "expirations", "uuid_controls", "website_usage"):
        try: conn.execute("UPDATE OR IGNORE %s SET tag=? WHERE tag=?" % table, (new_tag, old_tag))
        except sqlite3.Error: pass
    conn.execute("UPDATE OR REPLACE link_meta SET tag=? WHERE tag=?", (new_tag, old_tag))
    conn.commit()
    conn.close()


def changed_tag(old_tag, before):
    after = xray_files()
    if old_tag in after: return old_tag
    created = sorted(after - before)
    if len(created) != 1: raise RuntimeError("配置已修改，但无法识别新的配置文件名")
    migrate_tag(old_tag, created[0])
    return created[0]


def run_link_change(tag, change_type, value, extra=None):
    before = xray_files()
    args = ["change", tag, change_type, text_type(value)]
    if extra is not None: args.append(text_type(extra))
    run_xray(args, audit_action="link.change." + change_type, target=tag)
    return changed_tag(tag, before)


def update_link(payload):
    tag = (payload.get("tag") or "").strip()
    links = discover_links()
    if tag not in links: raise ValueError("找不到该 Xray 链接")
    link = links[tag]
    name = validate_name(payload.get("name"))
    backup = os.path.join(BACKUP_DIR, "%s.before-edit.%s" % (tag, now()))
    shutil.copy2(link["configPath"], backup)
    config = link["config"]
    kind = PROTOCOLS[config["protocolId"]]["kind"]
    current = tag
    if kind in ("direct", "reality", "shadowsocks", "socks") and payload.get("port") is not None:
        port = validate_port(payload.get("port"), current)
        if port != link["port"]: current = run_link_change(current, "port", port)
    if kind == "tls" and payload.get("host") and payload.get("host").strip().lower() != config.get("host"):
        current = run_link_change(current, "host", validate_domain(payload.get("host"), "TLS 域名"))
    if kind == "tls" and payload.get("path") and payload.get("path").strip() != config.get("path"):
        current = run_link_change(current, "path", validate_path(payload.get("path")))
    if kind == "reality" and payload.get("sni") and payload.get("sni").strip().lower() != config.get("sni"):
        current = run_link_change(current, "sni", validate_domain(payload.get("sni"), "Reality SNI"))
    if kind == "direct" and payload.get("headerType") and payload.get("headerType") != config.get("headerType"):
        current = run_link_change(current, "type", payload.get("headerType"))
    if kind == "shadowsocks" and payload.get("method") and payload.get("method") != config.get("method"):
        method = payload.get("method").strip().lower()
        if method not in SS_METHODS: raise ValueError("Shadowsocks 加密方式不受支持")
        current = run_link_change(current, "method", method)
    credential = (payload.get("credential") or "").strip()
    if credential:
        if kind in ("direct", "reality", "tls"):
            if not UUID_RE.match(credential): raise ValueError("UUID 格式不正确")
            current = run_link_change(current, "uuid", credential)
        elif kind in ("shadowsocks", "socks"):
            current = run_link_change(current, "passwd", validate_secret(credential))
    uri = extract_share_uri(current)
    save_link_meta(current, name, uri)
    apply_block_rules()
    return uri


def delete_link(payload):
    tag = (payload.get("tag") or "").strip()
    links = discover_links()
    if tag not in links: raise ValueError("找不到该 Xray 链接")
    backup = os.path.join(BACKUP_DIR, "%s.before-delete.%s" % (tag, now()))
    shutil.copy2(links[tag]["configPath"], backup)
    try:
        run_xray(["del", tag], audit_action="link.delete", target=tag)
    except RuntimeError:
        if os.path.exists(links[tag]["configPath"]): raise
        audit_command("link.delete.confirmed", tag, True, "脚本已删除配置；忽略其非零退出码")
    conn = db()
    for table in ("link_meta", "quotas", "expirations", "uuid_controls"):
        conn.execute("DELETE FROM %s WHERE tag=?" % table, (tag,))
    conn.commit()
    conn.close()


def service_status():
    def active(name):
        null = open(os.devnull, "w")
        try: return subprocess.call(["systemctl", "is-active", "--quiet", name], stdout=null, stderr=null) == 0
        finally: null.close()
    try: version = run_xray(["version"], timeout=10).splitlines()[0]
    except Exception: version = "Xray"
    conn = db()
    audit = list(conn.execute("SELECT action,target,success,output,created_at FROM command_audit ORDER BY id DESC LIMIT 12"))
    conn.close()
    return {"running": active("xray"), "caddyRunning": active("caddy"), "version": version,
            "script": "233boy v1.33", "protocols": [dict({"id": key}, **value) for key, value in PROTOCOLS.items()],
            "audit": audit}


def run_service_action(payload):
    action = (payload.get("action") or "").strip()
    if action == "link-url":
        tag = (payload.get("tag") or "").strip()
        if tag not in discover_links(): raise ValueError("找不到该 Xray 链接")
        uri = extract_share_uri(tag)
        if not uri: raise RuntimeError("该配置没有可用的分享链接")
        audit_command("link.url", tag, True, "已生成分享链接")
        return {"output": uri, "shareUri": uri}
    if action not in SERVICE_ACTIONS: raise ValueError("不允许执行该命令")
    if action == "test":
        test_all_configs()
        output = "全部 Xray 配置测试通过"
        audit_command("service.test", "xray", True, output)
    elif action in ("start", "stop", "restart"):
        try:
            output = run_process(["systemctl", action, "xray"], 45)
            audit_command("service." + action, "xray", True, output or "完成")
        except Exception as error:
            audit_command("service." + action, "xray", False, text_type(error))
            raise RuntimeError("Xray 服务操作失败")
    else:
        output = run_xray(SERVICE_ACTIONS[action], timeout=300, audit_action="service." + action, target="xray")
    return {"output": clean_output(output) or "命令执行完成"}


def read_logs():
    def tail(path, lines=160):
        try:
            with open(path, "rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 256 * 1024))
                return handle.read().decode("utf-8", "replace").splitlines()[-lines:]
        except IOError:
            return []
    return {"access": tail(LOG_PATH), "error": tail(XRAY_ERROR_LOG)}


def website_report(params):
    ranges = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
    range_key = (params.get("range") or ["24h"])[0]
    if range_key not in ranges: raise ValueError("时间范围不正确")
    stamp = now()
    start = stamp - ranges[range_key]
    links = discover_links()
    conditions = ["last_seen>=?"]
    values = [start]
    tag = (params.get("tag") or [""])[0].strip()
    if tag:
        if tag not in links: raise ValueError("找不到该链接")
        conditions.append("tag=?"); values.append(tag)
    address = (params.get("ip") or [""])[0].strip()
    if address:
        if not valid_ip(address): raise ValueError("来源 IP 格式不正确")
        conditions.append("ip=?"); values.append(address)
    device = (params.get("device") or [""])[0].strip().lower()
    if device:
        if not UUID_RE.match(device): raise ValueError("设备 UUID 格式不正确")
        conditions.append("email=?"); values.append(device_email(device))
    keyword = (params.get("q") or [""])[0].strip().lower()
    if keyword:
        if len(keyword) > 80: raise ValueError("搜索内容过长")
        conditions.append("target LIKE ? ESCAPE '\\'")
        values.append("%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    where = " AND ".join(conditions)
    conn = db()
    summary = conn.execute("SELECT COALESCE(SUM(connections),0) AS connections,COUNT(DISTINCT target) AS targets,COUNT(DISTINCT ip) AS ips,COUNT(DISTINCT CASE WHEN email!='' THEN email END) AS devices,COALESCE(MAX(last_seen),0) AS latest FROM website_usage WHERE " + where, values).fetchone()
    top_rows = list(conn.execute("SELECT target,port,SUM(connections) AS connections,COUNT(DISTINCT ip) AS unique_ips,COUNT(DISTINCT tag) AS unique_links,MAX(last_seen) AS last_seen FROM website_usage WHERE " + where + " GROUP BY target,port ORDER BY connections DESC,last_seen DESC LIMIT 12", values))
    visit_rows = list(conn.execute("SELECT tag,ip,target,port,network,email,SUM(connections) AS connections,MIN(first_seen) AS first_seen,MAX(last_seen) AS last_seen FROM website_usage WHERE " + where + " GROUP BY tag,ip,target,port,network,email ORDER BY last_seen DESC LIMIT 120", values))
    conn.close()
    device_map = {}
    for link_tag, link in links.items():
        for item in link.get("devices", []):
            device_map[(link_tag, device_email(item["uuid"]))] = item
    top_targets = []
    for row in top_rows:
        item = dict(row)
        item["isIp"] = valid_ip(item["target"])
        top_targets.append(item)
    visits = []
    for row in visit_rows:
        item = dict(row)
        known = device_map.get((item["tag"], item["email"]), {})
        item.update({
            "linkName": links.get(item["tag"], {}).get("name", item["tag"]),
            "deviceUuid": known.get("uuid"), "deviceCode": known.get("code"),
            "deviceLabel": known.get("deviceLabel") or "", "deviceKey": item["email"],
            "isIp": valid_ip(item["target"]),
        })
        item.pop("email", None)
        visits.append(item)
    return {
        "generatedAt": stamp, "range": range_key, "rangeStart": start,
        "retentionDays": WEBSITE_RETENTION_DAYS,
        "summary": {"connections": int(summary["connections"]), "targets": int(summary["targets"]),
                    "ips": int(summary["ips"]), "devices": int(summary["devices"]), "latest": int(summary["latest"])},
        "topTargets": top_targets, "visits": visits,
    }


def clear_website_history(payload):
    if not payload.get("confirm"):
        raise ValueError("需要确认后才能清除访问记录")
    conn = db()
    conn.execute("DELETE FROM website_usage")
    conn.commit()
    conn.close()
    audit_command("websites.clear", "all", True, "已清除访问网站历史")


def save_log_cursor(inode, offset, conn=None):
    owns_connection = conn is None
    if owns_connection: conn = db()
    conn.execute("INSERT OR REPLACE INTO log_cursor(path,inode,offset,updated_at) VALUES(?,?,?,?)", (LOG_PATH, inode, offset, now()))
    if owns_connection:
        conn.commit()
        conn.close()


def tail_log():
    while True:
        try:
            handle = open(LOG_PATH, "r")
            size = os.path.getsize(LOG_PATH)
            initial_end = size
            inode = os.stat(LOG_PATH).st_ino
            conn = db()
            cursor = conn.execute("SELECT inode,offset FROM log_cursor WHERE path=?", (LOG_PATH,)).fetchone()
            conn.close()
            resumed = bool(cursor and int(cursor["inode"]) == int(inode) and int(cursor["offset"]) <= size)
            if resumed:
                handle.seek(int(cursor["offset"]))
            else:
                handle.seek(max(0, size - 8 * 1024 * 1024))
            if handle.tell() > 0 and not resumed:
                handle.readline()
            processed = 0
            batch_conn = db()
            while True:
                line = handle.readline()
                if line:
                    record_access(line, batch_conn, handle.tell() > initial_end)
                    processed += 1
                    if processed % 250 == 0:
                        save_log_cursor(inode, handle.tell(), batch_conn)
                        batch_conn.commit()
                    continue
                save_log_cursor(inode, handle.tell(), batch_conn)
                batch_conn.commit()
                try:
                    if os.path.getsize(LOG_PATH) < handle.tell():
                        handle.close()
                        batch_conn.close()
                        break
                except OSError:
                    break
                time.sleep(1)
        except Exception:
            time.sleep(5)


def stats_loop():
    while True:
        try:
            update_stats()
        except Exception:
            pass
        time.sleep(30)


def snapshot():
    links_meta = discover_links()
    conn = db()
    stamp = now()
    counter_rows = {r["tag"]: r for r in conn.execute("SELECT * FROM counters")}
    quota_rows = {r["tag"]: r for r in conn.execute("SELECT * FROM quotas")}
    expiration_rows = {r["tag"]: r for r in conn.execute("SELECT * FROM expirations")}
    controls = {r["ip"]: r for r in conn.execute("SELECT * FROM ip_controls")}
    links, total_up, total_down = [], 0, 0
    for tag, meta in links_meta.items():
        row = counter_rows.get(tag)
        up, down = (int(row["total_up"]), int(row["total_down"])) if row else (0, 0)
        total_up += up
        total_down += down
        ips = []
        for item in conn.execute("SELECT ip,first_seen,last_seen,connections FROM ip_usage WHERE tag=? ORDER BY last_seen DESC LIMIT 40", (tag,)):
            control = controls.get(item["ip"], {})
            item = dict(item)
            item.update({"deviceLabel": control.get("device_label") or "", "blocked": bool(control.get("blocked"))})
            ips.append(item)
        quota_row = quota_rows.get(tag)
        expiration_row = expiration_rows.get(tag)
        quota = None
        if quota_row:
            period_start, next_reset = quota_period(int(quota_row["reset_day"]), stamp)
            used = max(0, up - int(quota_row["baseline_up"])) + max(0, down - int(quota_row["baseline_down"]))
            limit_bytes = int(quota_row["limit_bytes"])
            quota = {"enabled": bool(quota_row["enabled"]), "limitBytes": limit_bytes, "usedBytes": used,
                     "remainingBytes": max(0, limit_bytes - used), "resetDay": int(quota_row["reset_day"]),
                     "periodStart": period_start, "nextReset": next_reset, "disabled": bool(quota_row["disabled"]),
                     "lastError": quota_row.get("last_error")}
        expiration = None
        if expiration_row:
            expiration = {"enabled": bool(expiration_row["enabled"]), "expiresAt": int(expiration_row["expires_at"]),
                          "disabled": bool(expiration_row["disabled"]), "lastError": expiration_row.get("last_error")}
        public_meta = dict(meta)
        public_meta.pop("configPath", None)
        public_meta.update({"uplink": up, "downlink": down, "updatedAt": int(row["updated_at"]) if row else 0,
                            "ips": ips, "quota": quota, "expiration": expiration,
                            "disabled": bool((quota and quota["disabled"]) or (expiration and expiration["disabled"]))})
        links.append(public_meta)
    recent = []
    for row in conn.execute("SELECT tag,ip,first_seen,last_seen,connections FROM ip_usage ORDER BY last_seen DESC LIMIT 80"):
        item = dict(row)
        control = controls.get(item["ip"], {})
        item.update({"linkName": links_meta.get(item["tag"], {}).get("name", "Unknown"),
                     "deviceLabel": control.get("device_label") or "", "blocked": bool(control.get("blocked"))})
        recent.append(item)
    buckets = {}
    for row in conn.execute("SELECT bucket,SUM(up_bytes) AS up_bytes,SUM(down_bytes) AS down_bytes FROM samples WHERE bucket>=? GROUP BY bucket ORDER BY bucket", (stamp - 86400,)):
        buckets[str(row["bucket"])] = {"uplink": int(row["up_bytes"]), "downlink": int(row["down_bytes"])}
    month = server_period_key(stamp)
    traffic = conn.execute("SELECT * FROM server_traffic WHERE month=?", (month,)).fetchone()
    conn.close()
    used = int(traffic["total_rx"] + traffic["total_tx"]) if traffic else 0
    period_start, next_reset = server_period_bounds(stamp)
    manager = service_status()
    return {
        "server": {"name": "狗云", "host": SERVER_HOST, "online": manager["running"], "xray": manager["version"]},
        "generatedAt": stamp,
        "totals": {"uplink": total_up, "downlink": total_down, "traffic": total_up + total_down},
        "bandwidth": {"limitBytes": SERVER_LIMIT_BYTES, "usedBytes": used, "remainingBytes": max(0, SERVER_LIMIT_BYTES - used),
                      "periodStart": period_start, "nextReset": next_reset, "measuredSince": int(traffic["measured_since"]) if traffic else stamp,
                      "periodDays": SERVER_PERIOD_DAYS, "source": "local"},
        "links": links, "recentIps": recent, "series": buckets, "xrayManager": manager,
        "notice": "链接流量由 Xray 精确统计；来源 IP 来自连接日志。Xray 不会上报设备型号，可为 IP 添加设备备注并按备注批量封禁。服务器流量按 30 天账单周期统计，本机网卡口径可能与云厂商账单略有差异。",
    }


class Handler(BaseHTTPRequestHandler):
    def authorized(self):
        auth = self.headers.get("Authorization") or ""
        return bool(TOKEN) and auth == "Bearer " + TOKEN

    def read_payload(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 65536:
            raise ValueError("请求内容不正确")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.authorized():
            self.send_error(401); return
        parsed = urlparse(self.path)
        if parsed.path == "/v1/snapshot": self.send_json(200, snapshot())
        elif parsed.path == "/v1/xray/logs": self.send_json(200, read_logs())
        elif parsed.path == "/v1/websites":
            try: self.send_json(200, website_report(parse_qs(parsed.query)))
            except ValueError as error: self.send_json(400, {"error": text_type(error)})
            except Exception as error: self.send_json(500, {"error": "查询失败：" + text_type(error)[-240:]})
        else: self.send_error(404)

    def mutate(self, kind):
        if not self.authorized():
            self.send_error(401); return
        try:
            payload = self.read_payload()
            with MUTATION_LOCK:
                if kind == "quota": save_quota(payload)
                elif kind == "expiration": save_expiration(payload)
                elif kind == "ip": save_ip_control(payload)
                elif kind == "uuid": save_uuid_control(payload)
                elif kind == "create": create_link(payload)
                elif kind == "update": update_link(payload)
                elif kind == "delete": delete_link(payload)
                elif kind == "websites-clear":
                    clear_website_history(payload)
                    self.send_json(200, website_report({}))
                    return
                elif kind == "command":
                    result = run_service_action(payload)
                    result["snapshot"] = snapshot()
                    self.send_json(200, result)
                    return
            self.send_json(200, snapshot())
        except ValueError as error:
            self.send_json(400, {"error": text_type(error)})
        except Exception as error:
            self.send_json(500, {"error": "操作失败：" + text_type(error)[-240:]})

    def do_POST(self):
        if self.path == "/v1/quotas": self.mutate("quota")
        elif self.path == "/v1/expirations": self.mutate("expiration")
        elif self.path == "/v1/ips": self.mutate("ip")
        elif self.path == "/v1/uuids": self.mutate("uuid")
        elif self.path == "/v1/links": self.mutate("create")
        elif self.path == "/v1/xray/commands": self.mutate("command")
        else: self.send_error(404)

    def do_PATCH(self):
        if self.path == "/v1/links": self.mutate("update")
        else: self.send_error(404)

    def do_DELETE(self):
        if self.path == "/v1/links": self.mutate("delete")
        elif self.path == "/v1/websites": self.mutate("websites-clear")
        else: self.send_error(404)

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    init_db()
    purge_edge_ips()
    apply_uuid_controls()
    threading.Thread(target=tail_log).start()
    threading.Thread(target=stats_loop).start()
    HTTPServer((LISTEN, PORT), Handler).serve_forever()
