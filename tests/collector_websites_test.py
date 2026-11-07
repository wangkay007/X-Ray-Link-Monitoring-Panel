import importlib.util
import ipaddress
import json
import os
import tempfile
import time
import unittest


class WebsiteCollectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.temp.name
        config_dir = os.path.join(root, "conf")
        os.makedirs(config_dir)
        os.environ.update({
            "XRAY_MONITOR_DB": os.path.join(root, "monitor.db"),
            "XRAY_CONFIG_DIR": config_dir,
            "XRAY_MONITOR_BACKUPS": os.path.join(root, "backups"),
            "XRAY_CF_RANGES": os.path.join(root, "ranges"),
            "XRAY_MONITOR_STATIC_META": os.path.join(root, "meta.json"),
        })
        module_path = os.path.join(os.path.dirname(__file__), "..", "collector", "xray_monitor.py")
        spec = importlib.util.spec_from_file_location("collector_under_test", module_path)
        self.collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.collector)
        self.collector.init_db()
        self.uuid = "11111111-1111-4111-8111-111111111111"
        self.tag = "VMess-TCP-54321.json"
        payload = {"inbounds": [{
            "tag": self.tag, "port": 54321, "protocol": "vmess",
            "settings": {"clients": [{"id": self.uuid, "email": self.collector.device_email(self.uuid)}]},
            "streamSettings": {"network": "tcp"},
        }]}
        with open(os.path.join(config_dir, self.tag), "w") as handle:
            json.dump(payload, handle)

    def tearDown(self):
        self.temp.cleanup()

    def test_discovers_existing_hysteria_and_ss2022_without_exposing_secrets(self):
        for name, protocol, settings, stream in (
            ('Hysteria2-39906.json', 'hysteria', {'version': 2}, {'network': 'hysteria', 'security': 'tls'}),
            ('Shadowsocks-34829.json', 'shadowsocks', {'method': '2022-blake3-aes-256-gcm', 'password': 'private-key'}, {}),
        ):
            payload = {'inbounds': [{'tag': name, 'port': 34829, 'protocol': protocol, 'settings': settings, 'streamSettings': stream}]}
            with open(os.path.join(self.collector.XRAY_CONFIG_DIR, name), 'w') as handle:
                json.dump(payload, handle)
        links = self.collector.discover_links()
        self.assertEqual(links['Hysteria2-39906.json']['protocol'], 'Hysteria2')
        self.assertFalse(links['Hysteria2-39906.json']['managed'])
        self.assertEqual(links['Shadowsocks-34829.json']['config']['method'], '2022-blake3-aes-256-gcm')
        self.assertNotIn('private-key', json.dumps(links))
        uri = self.collector.extract_share_uri('Shadowsocks-34829.json')
        self.assertTrue(uri.startswith('ss://2022-blake3-aes-256-gcm:private-key@'))

    def test_collects_filters_and_clears_target_domains(self):
        timestamp = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime())
        line = "%s.123456 from 203.0.113.8:0 accepted tcp:chatgpt.com:443 [%s -> direct] email: %s\n" % (
            timestamp, self.tag, self.collector.device_email(self.uuid))
        self.collector.record_access(line)
        report = self.collector.website_report({"range": ["24h"], "q": ["chatgpt"]})
        self.assertEqual(report["summary"]["connections"], 1)
        self.assertEqual(report["topTargets"][0]["target"], "chatgpt.com")
        self.assertEqual(report["visits"][0]["deviceUuid"], self.uuid)
        for index in range(17):
            extra = "%s.123456 from 203.0.113.8:0 accepted tcp:page-%s.example.com:443 [%s -> direct] email: %s\n" % (
                timestamp, index, self.tag, self.collector.device_email(self.uuid))
            self.collector.record_access(extra)
        first_page = self.collector.website_report({"range": ["24h"], "page": ["1"]})
        second_page = self.collector.website_report({"range": ["24h"], "page": ["2"]})
        self.assertEqual(first_page["pagination"], {"page": 1, "pageSize": 15, "totalItems": 18, "totalPages": 2})
        self.assertEqual(len(first_page["visits"]), 15)
        self.assertEqual(len(second_page["visits"]), 3)
        self.assertFalse(set(item["target"] for item in first_page["visits"]) & set(item["target"] for item in second_page["visits"]))
        self.collector.clear_website_history({"confirm": True})
        self.assertEqual(self.collector.website_report({})["summary"]["connections"], 0)

    def test_recent_activity_deduplicates_sources_and_matches_configured_email(self):
        collector = self.collector
        path = os.path.join(collector.XRAY_CONFIG_DIR, self.tag)
        with open(path) as handle:
            payload = json.load(handle)
        payload["inbounds"][0]["settings"]["clients"][0]["email"] = "custom@example.test"
        with open(path, "w") as handle:
            json.dump(payload, handle)
        stamp = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime())
        for source, outbound in (("tcp:203.0.113.8", "direct"), ("[::ffff:203.0.113.8]", "direct"),
                                 ("[2001:db8::42]", "direct"), ("203.0.113.9", "block")):
            collector.record_access("%s.123456 from %s:1234 accepted tcp:example.com:443 [%s >> %s] email: custom@example.test\n" % (stamp, source, self.tag, outbound))
        report = collector.website_report({"device": [self.uuid]})
        self.assertEqual(report["summary"]["ips"], 2)
        self.assertEqual(report["summary"]["devices"], 1)
        collector.clear_website_history({"confirm": True})
        conn = collector.db()
        rows = list(conn.execute("SELECT ip,email,last_seen FROM client_activity WHERE tag=?", (self.tag,)))
        conn.close()
        meta = collector.discover_links()[self.tag]
        devices, activity = collector.link_activity(meta, rows, {}, "")
        self.assertEqual(activity["sourceCount"], 2)
        self.assertEqual(activity["activeUuidCount"], 1)
        self.assertEqual(devices[0]["recentIpCount"], 2)
        _, limited = collector.link_activity(meta, rows, {}, "203.0.113.8")
        self.assertEqual(limited["sourceCount"], 1)

    def test_source_limit_covers_both_families_except_bound_address(self):
        collector = self.collector
        candidates = ("0.0.0.0", "203.0.113.8", "203.0.113.9", "::", "2001:db8::8", "2001:db8::9")
        for allowed in ("203.0.113.8", "2001:db8::8"):
            rule = collector.source_limit_rule(self.tag, allowed)
            networks = [ipaddress.ip_network(value) for value in rule["source"]]
            self.assertEqual(collector.configured_source_limits({"routing": {"rules": [rule]}}), {self.tag: allowed})
            for candidate in candidates:
                self.assertEqual(any(ipaddress.ip_address(candidate) in net for net in networks), candidate != allowed)


if __name__ == "__main__":
    unittest.main()
