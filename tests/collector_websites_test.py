import importlib.util
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

    def test_collects_filters_and_clears_target_domains(self):
        timestamp = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime())
        line = "%s.123456 from 203.0.113.8:0 accepted tcp:chatgpt.com:443 [%s -> direct] email: %s\n" % (
            timestamp, self.tag, self.collector.device_email(self.uuid))
        self.collector.record_access(line)
        report = self.collector.website_report({"range": ["24h"], "q": ["chatgpt"]})
        self.assertEqual(report["summary"]["connections"], 1)
        self.assertEqual(report["topTargets"][0]["target"], "chatgpt.com")
        self.assertEqual(report["visits"][0]["deviceUuid"], self.uuid)
        self.collector.clear_website_history({"confirm": True})
        self.assertEqual(self.collector.website_report({})["summary"]["connections"], 0)


if __name__ == "__main__":
    unittest.main()
