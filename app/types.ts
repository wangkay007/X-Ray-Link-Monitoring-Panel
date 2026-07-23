export type IpUsage = {
  ip: string;
  first_seen: number;
  last_seen: number;
  connections: number;
  deviceLabel: string;
  blocked: boolean;
};

export type UuidDevice = {
  uuid: string;
  code: string;
  deviceLabel: string;
  disabled: boolean;
  lastError: string | null;
};

export type LinkUsage = {
  id: string;
  name: string;
  protocol: string;
  endpoint: string;
  transport: "direct" | "cdn";
  port: number;
  managed: boolean;
  shareUri: string | null;
  disabled: boolean;
  devices: UuidDevice[];
  uplink: number;
  downlink: number;
  updatedAt: number;
  ips: IpUsage[];
  quota: null | {
    enabled: boolean;
    limitBytes: number;
    usedBytes: number;
    remainingBytes: number;
    resetDay: number;
    periodStart: number;
    nextReset: number;
    disabled: boolean;
    lastError: string | null;
  };
  expiration: null | {
    enabled: boolean;
    expiresAt: number;
    disabled: boolean;
    lastError: string | null;
  };
};

export type MonitorSnapshot = {
  server: { name: string; host: string; online: boolean; xray: string };
  generatedAt: number;
  totals: { uplink: number; downlink: number; traffic: number };
  bandwidth: {
    limitBytes: number;
    usedBytes: number;
    remainingBytes: number;
    periodStart: number;
    nextReset: number;
    measuredSince: number;
    source: "local";
  };
  links: LinkUsage[];
  recentIps: Array<IpUsage & { tag: string; linkName: string }>;
  series: Record<string, { uplink: number; downlink: number }>;
  notice: string;
};
