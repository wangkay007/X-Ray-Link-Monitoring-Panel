"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import type { IpUsage, LinkUsage, MonitorSnapshot, UuidDevice } from "./types";

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / 1024 ** index;
  return `${amount >= 100 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function formatTime(stamp: number) {
  if (!stamp) return "暂无";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(stamp * 1000));
}

function formatDateTime(stamp: number) {
  if (!stamp) return "未设置";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(stamp * 1000));
}

function dateTimeLocal(stamp: number) {
  const date = new Date(stamp * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function relativeTime(stamp: number, now: number) {
  const seconds = Math.max(0, now - stamp);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint: string;
  tone?: "up" | "down";
}) {
  return (
    <div className="stat">
      <div className="stat-label">
        <span className={tone ? `direction ${tone}` : "direction total"} aria-hidden="true">
          {tone === "up" ? "↑" : tone === "down" ? "↓" : "Σ"}
        </span>
        {label}
      </div>
      <strong>{value}</strong>
      <span>{hint}</span>
    </div>
  );
}

function LinkRow({
  link,
  maxTraffic,
  selected,
  now,
  onSelect,
}: {
  link: LinkUsage;
  maxTraffic: number;
  selected: boolean;
  now: number;
  onSelect: () => void;
}) {
  const total = link.uplink + link.downlink;
  const width = Math.max(total > 0 ? 4 : 0, (total / Math.max(1, maxTraffic)) * 100);
  const activeIp = link.ips.find((ip) => now - ip.last_seen < 300);
  const deviceCount = link.devices?.length ?? 0;
  const disabled = link.disabled;
  const disabledReason = link.expiration?.disabled ? "已到期并停用" : link.quota?.disabled ? "已达到额度并停用" : "";
  return (
    <button className={`link-row ${selected ? "selected" : ""} ${disabled ? "disabled-link" : ""}`} onClick={onSelect} type="button">
      <span className="link-identity">
        <span className={`status-dot ${disabled ? "stopped" : activeIp ? "live" : ""}`} aria-hidden="true" />
        <span>
          <strong>{link.name}</strong>
          <small>{disabled ? disabledReason : link.protocol}</small>
        </span>
      </span>
      <span className="endpoint">{link.endpoint}</span>
      <span className="traffic-number">{formatBytes(total)}</span>
      <span className="traffic-track" aria-hidden="true">
        <span style={{ width: `${width}%` }} />
      </span>
      <span className={`ip-count ${disabled ? "danger-text" : ""}`}>
        <b>{deviceCount} UUID</b>
        <small>{disabled ? "已停用" : `${link.ips.length} IP`}</small>
      </span>
    </button>
  );
}

function QuotaEditor({ link, onSaved }: { link: LinkUsage; onSaved: (data: MonitorSnapshot) => void }) {
  const [enabled, setEnabled] = useState(link.quota?.enabled ?? false);
  const [limitGb, setLimitGb] = useState(link.quota ? String(Math.round(link.quota.limitBytes / 1024 ** 3 * 100) / 100) : "100");
  const [resetDay, setResetDay] = useState(String(link.quota?.resetDay ?? 1));
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const quota = link.quota;
  const percent = quota?.enabled ? Math.min(100, quota.usedBytes / Math.max(1, quota.limitBytes) * 100) : 0;

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const gigabytes = Number(limitGb);
    const day = Number(resetDay);
    if (enabled && (!Number.isFinite(gigabytes) || gigabytes <= 0)) {
      setMessage("启用额度时，流量上限必须大于 0 GB");
      return;
    }
    setSaving(true);
    setMessage("");
    try {
      const response = await fetch("/api/quotas", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tag: link.id,
          enabled,
          limitBytes: enabled ? Math.round(gigabytes * 1024 ** 3) : 0,
          resetDay: day,
        }),
      });
      const payload = await response.json();
      if (response.status === 401) return window.location.assign("/login");
      if (!response.ok) throw new Error(payload.error || "保存失败");
      onSaved(payload);
      setMessage("额度设置已保存");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="quota-editor" onSubmit={save}>
      <div className="quota-heading">
        <div>
          <strong>月度流量额度</strong>
          <span>达到上限后自动停用此链接</span>
        </div>
        <label className="switch-label">
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
          <span>{enabled ? "已启用" : "未启用"}</span>
        </label>
      </div>
      {quota?.enabled ? (
        <div className={`quota-progress ${quota.disabled ? "quota-exhausted" : ""}`}>
          <div><span>本周期已用</span><strong>{formatBytes(quota.usedBytes)} / {formatBytes(quota.limitBytes)}</strong></div>
          <span className="quota-track" aria-label={`已使用 ${Math.round(percent)}%`}><i style={{ width: `${percent}%` }} /></span>
          <small>{quota.disabled ? "链接已停用；提高额度或关闭限制后会自动恢复" : `剩余 ${formatBytes(quota.remainingBytes)} · 下次重置 ${formatTime(quota.nextReset)}`}</small>
        </div>
      ) : null}
      <div className="quota-fields">
        <label>
          <span>流量上限</span>
          <span className="input-unit"><input type="number" min="0.1" step="0.1" value={limitGb} onChange={(event) => setLimitGb(event.target.value)} disabled={!enabled} /><b>GB</b></span>
        </label>
        <label>
          <span>每月重置日</span>
          <select value={resetDay} onChange={(event) => setResetDay(event.target.value)} disabled={!enabled}>
            {Array.from({ length: 28 }, (_, index) => index + 1).map((day) => <option value={day} key={day}>{day} 日</option>)}
          </select>
        </label>
      </div>
      <div className="quota-actions">
        <span className={message && message !== "额度设置已保存" ? "form-error" : "form-success"} role="status">{message}</span>
        <button type="submit" disabled={saving}>{saving ? "保存中…" : "保存设置"}</button>
      </div>
      {!quota ? <p className="quota-note">首次保存后，从当前流量开始计算本周期用量。</p> : null}
    </form>
  );
}

function ExpiryEditor({ link, onSaved }: { link: LinkUsage; onSaved: (data: MonitorSnapshot) => void }) {
  const [minimum] = useState(() => Math.floor(Date.now() / 1000) + 60);
  const [enabled, setEnabled] = useState(link.expiration?.enabled ?? false);
  const [expiresAt, setExpiresAt] = useState(() => dateTimeLocal(link.expiration?.expiresAt || minimum + 30 * 86400));
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const expiration = link.expiration;

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const stamp = Math.floor(new Date(expiresAt).getTime() / 1000);
    if (enabled && (!Number.isFinite(stamp) || stamp <= Math.floor(Date.now() / 1000))) {
      setMessage("到期时间必须晚于当前时间");
      return;
    }
    setSaving(true);
    setMessage("");
    try {
      const response = await fetch("/api/expirations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: link.id, enabled, expiresAt: enabled ? stamp : 0 }),
      });
      const payload = await response.json();
      if (response.status === 401) return window.location.assign("/login");
      if (!response.ok) throw new Error(payload.error || "保存失败");
      onSaved(payload);
      setMessage("到期时间已保存");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className={`quota-editor expiry-editor ${expiration?.disabled ? "expiry-disabled" : ""}`} onSubmit={save}>
      <div className="quota-heading">
        <div>
          <strong>链接到期时间</strong>
          <span>到期后自动停用；续期或关闭限制后自动恢复</span>
        </div>
        <label className="switch-label">
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
          <span>{enabled ? "已启用" : "未启用"}</span>
        </label>
      </div>
      {expiration?.enabled ? (
        <div className="expiry-status">
          <span>{expiration.disabled ? "已到期" : "将在此时间到期"}</span>
          <strong>{formatDateTime(expiration.expiresAt)}</strong>
          {expiration.disabled ? <small>链接当前不可用；设置新的未来时间后会自动恢复</small> : null}
        </div>
      ) : null}
      <div className="expiry-field">
        <label>
          <span>到期日期与时间</span>
          <input type="datetime-local" value={expiresAt} min={dateTimeLocal(minimum)} onChange={(event) => setExpiresAt(event.target.value)} disabled={!enabled} />
        </label>
      </div>
      <div className="quota-actions">
        <span className={message && message !== "到期时间已保存" ? "form-error" : "form-success"} role="status">{message}</span>
        <button type="submit" disabled={saving}>{saving ? "保存中…" : "保存到期时间"}</button>
      </div>
    </form>
  );
}

function UuidDeviceRow({ device, tag, onSaved }: {
  device: UuidDevice;
  tag: string;
  onSaved: (data: MonitorSnapshot) => void;
}) {
  const [label, setLabel] = useState(device.deviceLabel);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");

  async function save(disabled = device.disabled) {
    setSaving(true);
    setMessage("");
    try {
      const response = await fetch("/api/uuids", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag, uuid: device.uuid, deviceLabel: label, disabled }),
      });
      const payload = await response.json();
      if (response.status === 401) return window.location.assign("/login");
      if (!response.ok) throw new Error(payload.error || "保存失败");
      onSaved(payload);
      setEditing(false);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={`uuid-device ${device.disabled ? "uuid-disabled" : ""}`}>
      <div className="uuid-device-head">
        <span className={`status-dot ${device.disabled ? "stopped" : "live"}`} aria-hidden="true" />
        <div>
          <strong>{device.deviceLabel || device.code}</strong>
          <small>{device.disabled ? "UUID 已禁用" : "UUID 可用"}</small>
        </div>
        <button type="button" onClick={() => navigator.clipboard.writeText(device.uuid)}>复制 UUID</button>
        <button type="button" onClick={() => setEditing((value) => !value)}>{editing ? "收起" : "管理"}</button>
      </div>
      <code title={device.uuid}>{device.uuid}</code>
      {editing ? (
        <div className="uuid-device-editor">
          <label><span>设备备注</span><input value={label} maxLength={40} onChange={(event) => setLabel(event.target.value)} placeholder="例如：我的 iPhone" /></label>
          <div>
            <button type="button" onClick={() => save(device.disabled)} disabled={saving}>保存备注</button>
            <button type="button" className={device.disabled ? "restore-button" : "danger-button"} onClick={() => save(!device.disabled)} disabled={saving}>{device.disabled ? "恢复 UUID" : "禁用 UUID"}</button>
          </div>
          {message ? <span className="form-error" role="status">{message}</span> : null}
        </div>
      ) : null}
    </div>
  );
}

function UuidDevices({ link, onSaved }: { link: LinkUsage; onSaved: (data: MonitorSnapshot) => void }) {
  const devices = link.devices ?? [];
  return (
    <section className="uuid-devices" aria-label="UUID 设备">
      <div className="uuid-title">
        <div><strong>UUID 设备</strong><span>展示此链接配置中的全部设备 UUID</span></div>
        <b>{devices.length} 个</b>
      </div>
      {devices.length ? devices.map((device) => (
        <UuidDeviceRow key={`${link.id}-${device.uuid}`} device={device} tag={link.id} onSaved={onSaved} />
      )) : (
        <div className="uuid-empty">
          <strong>此链接暂未读取到 UUID</strong>
          <span>在 Xray 的 clients 中加入设备后会自动显示。</span>
        </div>
      )}
      <p>禁用后不能新建连接；已建立的长连接可能需要短时间才会断开。</p>
    </section>
  );
}

function LinkEditor({ link, protocols, onSaved, onCancel }: {
  link?: LinkUsage;
  protocols: MonitorSnapshot["xrayManager"]["protocols"];
  onSaved: (data: MonitorSnapshot) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(link?.name ?? "");
  const [port, setPort] = useState(String(link?.port ?? 44301));
  const [protocolId, setProtocolId] = useState(link?.config.protocolId ?? "vless-reality");
  const [host, setHost] = useState(link?.config.host ?? "");
  const [path, setPath] = useState(link?.config.path ?? "");
  const [sni, setSni] = useState(link?.config.sni || "www.cloudflare.com");
  const [headerType, setHeaderType] = useState(link?.config.headerType || "none");
  const [method, setMethod] = useState(link?.config.method || "aes-256-gcm");
  const [username, setUsername] = useState(link?.config.username ?? "");
  const [credential, setCredential] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const definition = protocols.find((item) => item.id === protocolId);
  const kind = definition?.kind ?? "reality";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setMessage("");
    try {
      const response = await fetch("/api/links", {
        method: link ? "PATCH" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tag: link?.id, name, protocolId, port: Number(port), host, path, sni,
          headerType, method, username,
          ...(link ? { credential } : kind === "socks" || kind === "shadowsocks" ? { password: credential } : { uuid: credential }),
        }),
      });
      const payload = await response.json();
      if (response.status === 401) return window.location.assign("/login");
      if (!response.ok) throw new Error(payload.error || "保存失败");
      onSaved(payload);
      onCancel();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="link-editor" onSubmit={submit}>
      <div className="link-editor-heading">
        <div><strong>{link ? "编辑 Xray 链接" : "创建 Xray 链接"}</strong><span>{link ? "保存前自动备份，变更后立即生效" : "调用服务器现有 233boy Xray 脚本创建"}</span></div>
        <button type="button" onClick={onCancel} aria-label="关闭">×</button>
      </div>
      <div className="link-editor-fields">
        <label><span>名称</span><input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：手机专用" required maxLength={32} /></label>
        <label><span>协议</span><select value={protocolId} disabled={Boolean(link)} onChange={(event) => setProtocolId(event.target.value)}>{protocols.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
        {kind !== "tls" ? <label><span>入口端口</span><input type="number" min="1024" max="65535" value={port} onChange={(event) => setPort(event.target.value)} required /></label> : null}
        {kind === "tls" ? <label><span>TLS 域名</span><input value={host} onChange={(event) => setHost(event.target.value)} placeholder="已解析到本机的域名" required /></label> : null}
        {kind === "tls" ? <label><span>传输路径</span><input value={path} onChange={(event) => setPath(event.target.value)} placeholder="/xray-path" required /></label> : null}
        {kind === "reality" ? <label><span>Reality SNI</span><input value={sni} onChange={(event) => setSni(event.target.value)} required /></label> : null}
        {kind === "direct" ? <label><span>伪装类型</span><select value={headerType} onChange={(event) => setHeaderType(event.target.value)}><option value="none">none</option><option value="http">http</option><option value="wechat-video">wechat-video</option><option value="srtp">srtp</option></select></label> : null}
        {kind === "shadowsocks" ? <label><span>加密方式</span><select value={method} onChange={(event) => setMethod(event.target.value)}><option value="aes-256-gcm">aes-256-gcm</option><option value="aes-128-gcm">aes-128-gcm</option><option value="chacha20-poly1305">chacha20-poly1305</option><option value="xchacha20-poly1305">xchacha20-poly1305</option></select></label> : null}
        {kind === "socks" ? <label><span>Socks 用户名</span><input value={username} disabled={Boolean(link)} onChange={(event) => setUsername(event.target.value)} required /></label> : null}
        <label className="credential-field"><span>{kind === "shadowsocks" || kind === "socks" ? "密码" : "UUID"}</span><input type="password" value={credential} onChange={(event) => setCredential(event.target.value)} placeholder={link ? "留空则保持不变" : "留空自动生成"} required={kind === "socks" && !link} /></label>
      </div>
      <div className="link-editor-actions">
        <span className="form-error" role="status">{message}</span>
        <button type="submit" disabled={saving}>{saving ? "保存中…" : link ? "保存修改" : "创建链接"}</button>
      </div>
    </form>
  );
}

function XrayControlPanel({ data, onSaved, onError }: {
  data: MonitorSnapshot;
  onSaved: (data: MonitorSnapshot) => void;
  onError: (message: string) => void;
}) {
  const [running, setRunning] = useState("");
  const [output, setOutput] = useState("等待操作");
  const [logs, setLogs] = useState<{ access: string[]; error: string[] } | null>(null);
  const [logType, setLogType] = useState<"access" | "error">("error");
  const manager = data.xrayManager;

  async function command(action: string, confirmation?: string) {
    if (confirmation && !window.confirm(confirmation)) return;
    setRunning(action);
    onError("");
    try {
      const response = await fetch("/api/xray/commands", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const payload = await response.json();
      if (response.status === 401) return window.location.assign("/login");
      if (!response.ok) throw new Error(payload.error || "命令执行失败");
      setOutput(payload.output || "命令执行完成");
      onSaved(payload.snapshot);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "命令执行失败");
    } finally {
      setRunning("");
    }
  }

  async function loadLogs() {
    setRunning("logs");
    try {
      const response = await fetch("/api/xray/logs", { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "日志读取失败");
      setLogs(payload);
      setOutput("日志已刷新");
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "日志读取失败");
    } finally {
      setRunning("");
    }
  }

  const busy = Boolean(running);
  return (
    <section className="xray-console" aria-label="Xray 控制面板">
      <div className="console-heading">
        <div>
          <p className="eyebrow">XRAY CONTROL</p>
          <h2>Xray 服务控制</h2>
          <span>{manager.script} · {manager.version}</span>
        </div>
        <div className="service-badges">
          <span className={manager.running ? "ok" : "down"}><i />Xray {manager.running ? "运行中" : "已停止"}</span>
          <span className={manager.caddyRunning ? "ok" : "down"}><i />Caddy {manager.caddyRunning ? "运行中" : "已停止"}</span>
        </div>
      </div>
      <div className="console-grid">
        <div className="command-group">
          <strong>服务</strong><span>对应 xray start / stop / restart / test</span>
          <div className="command-buttons">
            <button type="button" disabled={busy || manager.running} onClick={() => command("start")}>启动</button>
            <button type="button" disabled={busy || !manager.running} onClick={() => command("restart")}>重启</button>
            <button type="button" disabled={busy} onClick={() => command("test")}>配置检测</button>
            <button type="button" className="danger-button" disabled={busy || !manager.running} onClick={() => command("stop", "停止后所有代理链接都会断开，确定继续吗？")}>停止</button>
          </div>
        </div>
        <div className="command-group">
          <strong>修复</strong><span>执行前建议先运行配置检测</span>
          <div className="command-buttons">
            <button type="button" disabled={busy} onClick={() => command("fix-all", "将修复全部 Xray 配置并可能重启服务，确定继续吗？")}>修复全部配置</button>
            <button type="button" disabled={busy} onClick={() => command("fix-config", "将重建 Xray 主配置，确定继续吗？")}>重建主配置</button>
            <button type="button" disabled={busy} onClick={() => command("fix-caddy", "将重建 Caddy 配置并重启 Caddy，确定继续吗？")}>修复 Caddy</button>
          </div>
        </div>
        <div className="command-group">
          <strong>更新</strong><span>对应 xray update，完成后服务可能自动重启</span>
          <div className="command-buttons">
            <button type="button" disabled={busy} onClick={() => command("update-core", "确定在线更新 Xray 内核吗？")}>更新内核</button>
            <button type="button" disabled={busy} onClick={() => command("update-script", "确定在线更新 233boy 脚本吗？")}>更新脚本</button>
            <button type="button" disabled={busy} onClick={() => command("update-data")}>更新规则数据</button>
            <button type="button" disabled={busy} onClick={() => command("update-caddy", "确定在线更新 Caddy 吗？")}>更新 Caddy</button>
          </div>
        </div>
      </div>
      <div className="console-output">
        <div><strong>最近输出</strong><span>{running ? "命令执行中，请勿关闭页面…" : output}</span><button type="button" onClick={loadLogs} disabled={busy}>{running === "logs" ? "读取中…" : "查看日志"}</button></div>
        {logs ? <div className="log-viewer"><div><button type="button" className={logType === "error" ? "active" : ""} onClick={() => setLogType("error")}>错误日志</button><button type="button" className={logType === "access" ? "active" : ""} onClick={() => setLogType("access")}>访问日志</button></div><pre>{(logs[logType] || []).join("\n") || "暂无日志"}</pre></div> : null}
      </div>
      <div className="audit-strip">
        <strong>操作审计</strong>
        {manager.audit.length ? manager.audit.slice(0, 5).map((item, index) => <span key={`${item.created_at}-${index}`} className={item.success ? "success" : "failed"}>{item.success ? "成功" : "失败"} · {item.action} · {formatTime(item.created_at)}</span>) : <span>尚无面板命令记录</span>}
      </div>
    </section>
  );
}

function IpControlRow({ item, now, onSaved }: {
  item: IpUsage & { tag: string; linkName: string };
  now: number;
  onSaved: (data: MonitorSnapshot) => void;
}) {
  const [label, setLabel] = useState(item.deviceLabel ?? "");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const isActive = now - item.last_seen < 300;

  async function save(blocked = item.blocked, scope: "ip" | "device" = "ip") {
    setSaving(true);
    setMessage("");
    try {
      const response = await fetch("/api/ips", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ip: item.ip, deviceLabel: label, blocked, scope }),
      });
      const payload = await response.json();
      if (response.status === 401) return window.location.assign("/login");
      if (!response.ok) throw new Error(payload.error || "保存失败");
      onSaved(payload);
      setEditing(false);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className={`ip-card ${item.blocked ? "blocked" : ""}`}>
      <div className="ip-row">
        <span className={`status-dot ${item.blocked ? "stopped" : isActive ? "live" : ""}`} aria-hidden="true" />
        <div>
          <strong>{item.ip}</strong>
          <small>{item.deviceLabel || "未知设备"} · {item.linkName} · {item.connections} 次连接</small>
        </div>
        <time dateTime={new Date(item.last_seen * 1000).toISOString()} title={formatTime(item.last_seen)}>{relativeTime(item.last_seen, now)}</time>
      </div>
      {editing ? (
        <div className="ip-control-editor">
          <label><span>设备备注</span><input value={label} maxLength={40} onChange={(event) => setLabel(event.target.value)} placeholder="例如：我的 iPhone" /></label>
          <div>
            <button type="button" onClick={() => save(item.blocked)} disabled={saving}>保存备注</button>
            <button type="button" className={item.blocked ? "restore-button" : "danger-button"} onClick={() => save(!item.blocked, "ip")} disabled={saving}>{item.blocked ? "解除 IP" : "禁用 IP"}</button>
            {label ? <button type="button" className={item.blocked ? "restore-button" : "danger-button"} onClick={() => save(!item.blocked, "device")} disabled={saving}>{item.blocked ? "解除设备" : "禁用设备"}</button> : null}
            <button type="button" onClick={() => { setEditing(false); setLabel(item.deviceLabel ?? ""); }}>取消</button>
          </div>
          {message ? <span className="form-error">{message}</span> : null}
        </div>
      ) : (
        <button type="button" className="ip-manage-button" onClick={() => setEditing(true)}>{item.blocked ? "已禁用 · 管理" : "备注 / 禁用"}</button>
      )}
    </div>
  );
}

export default function Dashboard() {
  const [data, setData] = useState<MonitorSnapshot | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<string>("all");
  const [creating, setCreating] = useState(false);
  const [editingLink, setEditingLink] = useState(false);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/monitor", { cache: "no-store" });
      if (response.status === 401) return window.location.assign("/login");
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "数据加载失败");
      setData(payload);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "数据加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(load, 0);
    const refresh = window.setInterval(load, 30_000);
    const clock = window.setInterval(() => setNow(Math.floor(Date.now() / 1000)), 30_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(refresh);
      window.clearInterval(clock);
    };
  }, [load]);

  const links = data?.links ?? [];
  const maxTraffic = Math.max(1, ...links.map((link) => link.uplink + link.downlink));
  const selectedLink = selected === "all" ? null : links.find((link) => link.id === selected);
  const ips = selectedLink
    ? selectedLink.ips.map((ip) => ({ ...ip, linkName: selectedLink.name, tag: selectedLink.id }))
    : data?.recentIps ?? [];
  const activeIps = useMemo(
    () => new Set((data?.recentIps ?? []).filter((ip) => now - ip.last_seen < 300).map((ip) => ip.ip)).size,
    [data, now],
  );

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.assign("/login");
  }

  async function deleteLink(link: LinkUsage) {
    if (!window.confirm(`确定删除“${link.name}”吗？删除后客户端将立即无法连接。`)) return;
    try {
      const response = await fetch("/api/links", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: link.id }),
      });
      const payload = await response.json();
      if (response.status === 401) return window.location.assign("/login");
      if (!response.ok) throw new Error(payload.error || "删除失败");
      setData(payload);
      setSelected("all");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除失败");
    }
  }

  async function copyLink(link: LinkUsage) {
    try {
      let uri = link.shareUri;
      if (!uri) {
        const response = await fetch("/api/xray/commands", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "link-url", tag: link.id }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "链接生成失败");
        uri = payload.shareUri;
        setData(payload.snapshot);
      }
      if (!uri) throw new Error("该配置没有可复制的客户端链接");
      await navigator.clipboard.writeText(uri);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "复制失败");
    }
  }

  const bandwidth = data?.bandwidth;
  const bandwidthPercent = bandwidth ? Math.min(100, bandwidth.usedBytes / Math.max(1, bandwidth.limitBytes) * 100) : 0;

  if (loading && !data) {
    return (
      <main className="shell loading-shell" aria-busy="true">
        <div className="loading-mark" />
        <p>正在连接狗云监控数据…</p>
      </main>
    );
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">D</span>
          <div>
            <h1>狗云监控</h1>
            <p>个人 Xray 流量与来源 IP</p>
          </div>
        </div>
        <div className="server-state">
          <span className={`status-dot ${data?.server.online ? "live" : ""}`} aria-hidden="true" />
          <span>
            <strong>{data?.server.online ? "服务正常" : "服务离线"}</strong>
            <small>{data ? `${data.server.host} · Xray ${data.server.xray}` : "等待数据"}</small>
          </span>
          <button type="button" onClick={load} aria-label="立即刷新">
            刷新
          </button>
          <button type="button" onClick={logout} aria-label="退出登录">
            退出
          </button>
        </div>
      </header>

      {error ? (
        <div className="error-banner" role="alert">
          <span>数据暂时不可用：{error}</span>
          <button type="button" onClick={load}>重试</button>
        </div>
      ) : null}

      <section className="overview" aria-label="流量总览">
        <div className="overview-heading">
          <div>
            <p>累计流量</p>
            <h2>{formatBytes(data?.totals.traffic ?? 0)}</h2>
          </div>
          <span>采集服务启动以来</span>
          {bandwidth ? <div className="server-quota">
            <div><span>服务器本周期剩余</span><strong>{formatBytes(bandwidth.remainingBytes)}</strong></div>
            <span className="quota-track"><i style={{ width: `${bandwidthPercent}%` }} /></span>
            <small>本机已统计 {formatBytes(bandwidth.usedBytes)} / {formatBytes(bandwidth.limitBytes)} · {bandwidth.periodDays} 天周期 · {formatTime(bandwidth.nextReset)} 重置</small>
          </div> : null}
        </div>
        <div className="stats">
          <Stat label="上行" value={formatBytes(data?.totals.uplink ?? 0)} hint="客户端发往服务器" tone="up" />
          <Stat label="下行" value={formatBytes(data?.totals.downlink ?? 0)} hint="服务器发往客户端" tone="down" />
          <Stat label="活跃 IP" value={String(activeIps)} hint="最近 5 分钟" />
          <Stat label="代理链接" value={String(links.length)} hint={`${links.filter((link) => link.ips.length > 0).length} 条已有记录`} />
        </div>
      </section>

      {data ? <XrayControlPanel data={data} onSaved={setData} onError={setError} /> : null}

      <section className="workspace">
        <div className="links-panel">
          <div className="section-title">
            <div>
              <h2>链接用量</h2>
              <p>点击链接筛选右侧 IP 记录</p>
            </div>
            <button type="button" onClick={() => setCreating((value) => !value)}>{creating ? "收起" : "+ 创建链接"}</button>
          </div>
          {creating && data ? <LinkEditor protocols={data.xrayManager.protocols} onSaved={setData} onCancel={() => setCreating(false)} /> : null}
          <div className="link-table-head" aria-hidden="true">
            <span>链接</span><span>入口</span><span>总流量</span><span>相对用量</span><span>设备 / 来源</span>
          </div>
          <div className="link-list">
            {links
              .slice()
              .sort((a, b) => b.uplink + b.downlink - a.uplink - a.downlink)
              .map((link) => (
                <LinkRow
                  key={link.id}
                  link={link}
                  maxTraffic={maxTraffic}
                  selected={selected === link.id}
                  now={now}
                  onSelect={() => setSelected(selected === link.id ? "all" : link.id)}
                />
              ))}
          </div>
        </div>

        <aside className="ip-panel">
          <div className="section-title">
            <div>
              <h2>{selectedLink ? `${selectedLink.name} 的 IP` : "最近来源 IP"}</h2>
              <p>{selectedLink ? "当前链接的历史使用来源" : "按最后连接时间排序"}</p>
            </div>
            {selectedLink ? <button type="button" onClick={() => setSelected("all")}>清除</button> : null}
          </div>
          {selectedLink ? <UuidDevices key={`uuid-${selectedLink.id}`} link={selectedLink} onSaved={setData} /> : null}
          {selectedLink ? <QuotaEditor key={selectedLink.id} link={selectedLink} onSaved={setData} /> : null}
          {selectedLink ? <ExpiryEditor key={`expiry-${selectedLink.id}`} link={selectedLink} onSaved={setData} /> : null}
          {selectedLink?.managed ? (
            <div className="managed-link-actions">
              <div><strong>Xray 配置管理</strong><span>已接管服务器现有配置；编辑前自动备份</span></div>
              <div>
                <button type="button" onClick={() => copyLink(selectedLink)}>复制链接</button>
                <button type="button" onClick={() => setEditingLink((value) => !value)}>编辑</button>
                <button type="button" className="danger-button" onClick={() => deleteLink(selectedLink)}>删除</button>
              </div>
            </div>
          ) : null}
          {selectedLink?.managed && editingLink && data ? <LinkEditor link={selectedLink} protocols={data.xrayManager.protocols} onSaved={setData} onCancel={() => setEditingLink(false)} /> : null}
          <div className="ip-list">
            {ips.length ? ips.slice(0, 14).map((item) => {
              return <IpControlRow key={`${item.tag}-${item.ip}`} item={item} now={now} onSaved={setData} />;
            }) : (
              <div className="empty-state">
                <strong>暂无 IP 记录</strong>
                <span>新连接建立后会自动出现在这里</span>
              </div>
            )}
          </div>
        </aside>
      </section>

      <section className="data-note">
        <span className="info-mark" aria-hidden="true">i</span>
        <div>
          <strong>数据精度说明</strong>
          <p>{data?.notice ?? "链接流量与来源 IP 将分别采集。"}</p>
        </div>
        <span className="last-sync">最后同步 {data ? relativeTime(data.generatedAt, now) : "—"}</span>
      </section>

      <footer>
        <span>Dog Cloud Monitor</span>
        <span>仅本人可访问 · 数据不经过浏览器存储</span>
      </footer>
    </main>
  );
}
