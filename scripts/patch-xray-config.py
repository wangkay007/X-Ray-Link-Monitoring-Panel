#!/usr/bin/env python3
"""Enable the local Xray API and statistics required by the monitor."""

import argparse
import json
import os
import stat
import tempfile


API_SERVICES = ("HandlerService", "LoggerService", "StatsService", "RoutingService")


def append_unique(items, value, key):
    if not any(item.get(key) == value.get(key) for item in items):
        items.append(value)


def patch(config, api_port, access_log):
    api = config.setdefault("api", {})
    api["tag"] = "api"
    services = api.setdefault("services", [])
    for service in API_SERVICES:
        if service not in services:
            services.append(service)

    config.setdefault("stats", {})

    policy = config.setdefault("policy", {})
    level_zero = policy.setdefault("levels", {}).setdefault("0", {})
    level_zero["statsUserUplink"] = True
    level_zero["statsUserDownlink"] = True
    system = policy.setdefault("system", {})
    system["statsInboundUplink"] = True
    system["statsInboundDownlink"] = True
    system["statsOutboundUplink"] = True
    system["statsOutboundDownlink"] = True

    log = config.setdefault("log", {})
    log.setdefault("access", access_log)
    log.setdefault("error", "/var/log/xray/error.log")
    log.setdefault("loglevel", "warning")

    inbounds = config.setdefault("inbounds", [])
    api_inbound = next((item for item in inbounds if item.get("tag") == "api"), None)
    if api_inbound:
        api_inbound.update(
            {
                "listen": "127.0.0.1",
                "port": api_port,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            }
        )
    else:
        inbounds.append(
            {
                "listen": "127.0.0.1",
                "port": api_port,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
                "tag": "api",
            }
        )

    outbounds = config.setdefault("outbounds", [])
    append_unique(outbounds, {"protocol": "blackhole", "tag": "block"}, "tag")

    routing = config.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    if not any(
        item.get("outboundTag") == "api" and "api" in item.get("inboundTag", [])
        for item in rules
    ):
        rules.insert(
            0,
            {
                "type": "field",
                "inboundTag": ["api"],
                "outboundTag": "api",
            },
        )

    return config


def atomic_write(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    original = os.stat(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".xray-monitor-", suffix=".json", dir=directory)
    try:
        os.fchmod(descriptor, stat.S_IMODE(original.st_mode))
        if hasattr(os, "fchown"):
            os.fchown(descriptor, original.st_uid, original.st_gid)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("--api-port", type=int, default=47495)
    parser.add_argument("--access-log", default="/var/log/xray/access.log")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    atomic_write(args.config, patch(config, args.api_port, args.access_log))
    print("Xray API and statistics configuration is ready")


if __name__ == "__main__":
    main()
