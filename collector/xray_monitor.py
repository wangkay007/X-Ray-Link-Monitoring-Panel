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
    from urllib.parse import quote as url_quote
except ImportError:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from urllib import quote as url_quote

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

ACCESS_RE = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\.\d+ from (\[[^\]]+\]|[^: ]+):\d+ .* \[([^ ]+) ")
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
            if not tag or protocol not in ("vless", "vmess"):
                continue
            stream = inbound.get("streamSettings", {})
            network = stream.get("network", "tcp")
            security = stream.get("security")
            protocol_label = protocol.upper()
            if security == "reality":
                protocol_label += " Reality"
            elif network == "ws":
                protocol_label += " WS TLS"
            port = int(inbound.get("port", 0) or 0)
            static = STATIC_META.get(tag)
            metadata = meta_rows.get(tag)
            name = metadata["name"] if metadata else (static[0] if static else tag.replace(".json", ""))
            endpoint = SERVER_HOST + ":" + str(port)
            transport = "direct"
            if static:
                endpoint, transport = static[1], static[2]
            elif network == "ws":
                headers = stream.get("wsSettings", {}).get("headers", {})
                endpoint = headers.get("Host") or headers.get("host") or endpoint
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
            result[tag] = {
                "id": tag, "name": name, "protocol": protocol_label,
                "endpoint": endpoint, "transport": transport, "port": port,
                "managed": bool(metadata and metadata["managed"]),
                "shareUri": metadata.get("share_uri") if metadata else None,
                "devices": devices,
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


def record_access(line):
    match = ACCESS_RE.match(line)
    if not match:
        return
    date_text, address, tag = match.groups()
    if os.path.basename(tag) != tag or not os.path.isfile(os.path.join(XRAY_CONFIG_DIR, tag)):
        return
    address = address.strip("[]")
    if is_cloudflare_ip(address):
        return
    try:
        stamp = int(time.mktime(time.strptime(date_text, "%Y/%m/%d %H:%M:%S")))
    except Exception:
        stamp = now()
    conn = db()
    row = conn.execute("SELECT connections FROM ip_usage WHERE tag=? AND ip=?", (tag, address)).fetchone()
    if row:
        conn.execute("UPDATE ip_usage SET last_seen=?,connections=? WHERE tag=? AND ip=?", (stamp, row["connections"] + 1, tag, address))
    else:
        conn.execute("INSERT INTO ip_usage(tag,ip,first_seen,last_seen,connections) VALUES(?,?,?,?,1)", (tag, address, stamp, stamp))
    conn.execute("INSERT OR IGNORE INTO ip_controls(ip,device_label,blocked,updated_at) VALUES(?,'',0,?)", (address, stamp))
    conn.commit()
    conn.close()


def run_xray(args):
    return subprocess.check_output([XRAY] + args, stderr=subprocess.STDOUT).decode("utf-8", "replace").strip()


def validate_name_port_sni(payload, current_tag=None):
    name = (payload.get("name") or "").strip()
    sni = (payload.get("sni") or "www.cloudflare.com").strip().lower()
    try:
        port = int(payload.get("port"))
    except Exception:
        raise ValueError("端口格式不正确")
    if not name or len(name) > 32:
        raise ValueError("名称需为 1 到 32 个字符")
    if port < 1024 or port > 65535:
        raise ValueError("端口需在 1024 到 65535 之间")
    if not DOMAIN_RE.match(sni) or ".." in sni:
        raise ValueError("伪装域名格式不正确")
    for tag, link in discover_links().items():
        if tag != current_tag and link["port"] == port:
            raise ValueError("该端口已被其他 Xray 链接使用")
    return name, port, sni


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


def share_uri(client_id, port, sni, public_key, short_id, name):
    query = "encryption=none&flow=xtls-rprx-vision&security=reality&sni=%s&fp=chrome&pbk=%s&sid=%s&type=tcp" % (
        url_quote(sni.encode("utf-8")), url_quote(public_key.encode("utf-8")), short_id)
    return "vless://%s@%s:%s?%s#%s" % (client_id, SERVER_HOST, port, query, url_quote(name.encode("utf-8")))


def create_link(payload):
    name, port, sni = validate_name_port_sni(payload)
    client_id = run_xray(["uuid"]).splitlines()[-1].strip()
    keys = run_xray(["x25519"])
    private_match = re.search(r"PrivateKey:\s*(\S+)", keys)
    public_match = re.search(r"Password \(PublicKey\):\s*(\S+)", keys)
    if not private_match or not public_match:
        raise RuntimeError("无法生成 Reality 密钥")
    private_key, public_key = private_match.group(1), public_match.group(1)
    short_id = binascii.hexlify(os.urandom(8)).decode("ascii")
    tag = "MONITOR-VLESS-%s.json" % hashlib.sha1((name + str(port) + str(now())).encode("utf-8")).hexdigest()[:12]
    path = os.path.join(XRAY_CONFIG_DIR, tag)
    config = {"inbounds": [{
        "listen": "0.0.0.0", "port": port, "protocol": "vless", "tag": tag,
        "settings": {"clients": [{"id": client_id, "email": device_email(client_id), "flow": "xtls-rprx-vision"}], "decryption": "none"},
        "sniffing": {"enabled": True, "destOverride": ["http", "tls"], "routeOnly": True},
        "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
            "dest": sni + ":443", "serverNames": [sni], "privateKey": private_key,
            "publicKey": public_key, "shortIds": [short_id]
        }}
    }]}
    uri = share_uri(client_id, port, sni, public_key, short_id, name)
    atomic_write_json(path, config)
    try:
        test_all_configs()
        run_xray(["api", "adi", "--server=" + API_SERVER, path])
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        raise
    conn = db()
    conn.execute("INSERT INTO link_meta(tag,name,managed,share_uri,created_at,updated_at) VALUES(?,?,1,?,?,?)", (tag, name, uri, now(), now()))
    conn.commit()
    conn.close()
    apply_block_rules()
    return uri


def update_link(payload):
    tag = payload.get("tag")
    links = discover_links()
    if tag not in links or not links[tag]["managed"]:
        raise ValueError("仅支持编辑由后台创建的链接")
    name, port, sni = validate_name_port_sni(payload, tag)
    path = links[tag]["configPath"]
    config = read_json(path)
    inbound = config["inbounds"][0]
    reality = inbound["streamSettings"]["realitySettings"]
    client_id = inbound["settings"]["clients"][0]["id"]
    public_key = reality.get("publicKey")
    if not public_key:
        output = run_xray(["x25519", "-i", reality["privateKey"]])
        match = re.search(r"Password \(PublicKey\):\s*(\S+)", output)
        public_key = match.group(1) if match else ""
    short_id = (reality.get("shortIds") or [""])[0]
    uri = share_uri(client_id, port, sni, public_key, short_id, name)
    backup = os.path.join(BACKUP_DIR, "%s.%s" % (tag, now()))
    shutil.copy2(path, backup)
    inbound["port"] = port
    reality["dest"] = sni + ":443"
    reality["serverNames"] = [sni]
    atomic_write_json(path, config)
    try:
        test_all_configs()
        run_xray(["api", "rmi", "--server=" + API_SERVER, tag])
        run_xray(["api", "adi", "--server=" + API_SERVER, path])
    except Exception:
        shutil.copy2(backup, path)
        try:
            run_xray(["api", "adi", "--server=" + API_SERVER, path])
        except Exception:
            pass
        raise
    conn = db()
    conn.execute("UPDATE link_meta SET name=?,share_uri=?,updated_at=? WHERE tag=?", (name, uri, now(), tag))
    conn.commit()
    conn.close()
    apply_block_rules()
    return uri


def delete_link(payload):
    tag = payload.get("tag")
    links = discover_links()
    if tag not in links or not links[tag]["managed"]:
        raise ValueError("仅支持删除由后台创建的链接")
    path = links[tag]["configPath"]
    backup = os.path.join(BACKUP_DIR, "%s.deleted.%s" % (tag, now()))
    run_xray(["api", "rmi", "--server=" + API_SERVER, tag])
    shutil.move(path, backup)
    conn = db()
    conn.execute("DELETE FROM link_meta WHERE tag=?", (tag,))
    conn.execute("DELETE FROM quotas WHERE tag=?", (tag,))
    conn.execute("DELETE FROM expirations WHERE tag=?", (tag,))
    conn.execute("DELETE FROM uuid_controls WHERE tag=?", (tag,))
    conn.commit()
    conn.close()


def tail_log():
    while True:
        try:
            handle = open(LOG_PATH, "r")
            size = os.path.getsize(LOG_PATH)
            handle.seek(max(0, size - 8 * 1024 * 1024))
            if size > 8 * 1024 * 1024:
                handle.readline()
            while True:
                line = handle.readline()
                if line:
                    record_access(line)
                    continue
                try:
                    if os.path.getsize(LOG_PATH) < handle.tell():
                        handle.close()
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
    return {
        "server": {"name": "狗云", "host": SERVER_HOST, "online": bool(counter_rows), "xray": "26.3.27"},
        "generatedAt": stamp,
        "totals": {"uplink": total_up, "downlink": total_down, "traffic": total_up + total_down},
        "bandwidth": {"limitBytes": SERVER_LIMIT_BYTES, "usedBytes": used, "remainingBytes": max(0, SERVER_LIMIT_BYTES - used),
                      "periodStart": period_start, "nextReset": next_reset, "measuredSince": int(traffic["measured_since"]) if traffic else stamp,
                      "periodDays": SERVER_PERIOD_DAYS, "source": "local"},
        "links": links, "recentIps": recent, "series": buckets,
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
        if self.path != "/v1/snapshot":
            self.send_error(404); return
        if not self.authorized():
            self.send_error(401); return
        self.send_json(200, snapshot())

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
        else: self.send_error(404)

    def do_PATCH(self):
        if self.path == "/v1/links": self.mutate("update")
        else: self.send_error(404)

    def do_DELETE(self):
        if self.path == "/v1/links": self.mutate("delete")
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
