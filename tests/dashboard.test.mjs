import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

async function loadWorker() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  return (await import(workerUrl.href)).default;
}

function runtime() {
  return {
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  };
}

function context() {
  return {
    waitUntil() {},
    passThroughOnException() {},
  };
}

test("redirects an anonymous dashboard request to login", async () => {
  const worker = await loadWorker();
  const response = await worker.fetch(
    new Request("http://localhost/"),
    runtime(),
    context(),
  );

  assert.ok([302, 303, 307, 308].includes(response.status));
  assert.equal(new URL(response.headers.get("location"), "http://localhost").pathname, "/login");
});

test("renders the login page without exposing server credentials", async () => {
  const worker = await loadWorker();
  const response = await worker.fetch(
    new Request("http://localhost/login", {
      headers: { accept: "text/html" },
    }),
    runtime(),
    context(),
  );

  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /狗云监控/);
  assert.match(html, /登录后查看服务器状态/);
  assert.doesNotMatch(html, /MONITOR_TOKEN|SESSION_SECRET|ADMIN_PASSWORD/);
});

test("keeps multi-client UUID discovery and rendering enabled", async () => {
  const [collector, dashboard] = await Promise.all([
    readFile(new URL("collector/xray_monitor.py", root), "utf8"),
    readFile(new URL("app/Dashboard.tsx", root), "utf8"),
  ]);

  assert.match(collector, /for client in inbound\.get\("settings", \{\}\)\.get\("clients", \[\]\)/);
  assert.match(dashboard, /devices\.map\(\(device\)/);
  assert.match(dashboard, /展示此链接配置中的全部设备 UUID/);
});

test("uses a configurable rolling server traffic period", async () => {
  const [collector, dashboard] = await Promise.all([
    readFile(new URL("collector/xray_monitor.py", root), "utf8"),
    readFile(new URL("app/Dashboard.tsx", root), "utf8"),
  ]);

  assert.match(collector, /XRAY_MONITOR_RESET_ANCHOR/);
  assert.match(collector, /XRAY_MONITOR_PERIOD_DAYS/);
  assert.match(collector, /server_period_bounds/);
  assert.match(dashboard, /服务器本周期剩余/);
  assert.doesNotMatch(dashboard, /服务器本月剩余/);
});

test("exposes validated Xray management without a raw shell endpoint", async () => {
  const [collector, dashboard, commandRoute] = await Promise.all([
    readFile(new URL("collector/xray_monitor.py", root), "utf8"),
    readFile(new URL("app/Dashboard.tsx", root), "utf8"),
    readFile(new URL("app/api/xray/commands/route.ts", root), "utf8"),
  ]);

  assert.match(collector, /SERVICE_ACTIONS = \{/);
  assert.match(collector, /if action not in SERVICE_ACTIONS/);
  assert.match(collector, /"vless-xhttp-tls"/);
  assert.match(collector, /"trojan-grpc-tls"/);
  assert.doesNotMatch(collector, /shell\s*=\s*True/);
  assert.match(dashboard, /Xray 服务控制/);
  assert.match(dashboard, /修复全部配置/);
  assert.match(commandRoute, /isAuthenticated/);
});

test("collects target domains behind an authenticated website activity API", async () => {
  const [collector, dashboard, route] = await Promise.all([
    readFile(new URL("collector/xray_monitor.py", root), "utf8"),
    readFile(new URL("app/Dashboard.tsx", root), "utf8"),
    readFile(new URL("app/api/websites/route.ts", root), "utf8"),
  ]);

  assert.match(collector, /CREATE TABLE IF NOT EXISTS website_usage/);
  assert.match(collector, /def website_report/);
  assert.match(collector, /WEBSITE_RETENTION_DAYS/);
  assert.match(dashboard, /访问网站/);
  assert.match(dashboard, /UUID 设备/);
  assert.match(dashboard, /HTTPS 内容仍然加密/);
  assert.match(route, /isAuthenticated/);
});
