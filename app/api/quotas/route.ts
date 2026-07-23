import { isAuthenticated } from "@/app/lib/auth";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  if (!(await isAuthenticated())) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }
  const endpoint = process.env.MONITOR_ENDPOINT?.replace(/\/v1\/snapshot$/, "/v1/quotas");
  const token = process.env.MONITOR_TOKEN;
  if (!endpoint || !token) return Response.json({ error: "监控数据源尚未配置" }, { status: 503 });

  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return Response.json({ error: "请求格式不正确" }, { status: 400 });
  }

  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    });
    const text = await response.text();
    return new Response(text, {
      status: response.status,
      headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
    });
  } catch {
    return Response.json({ error: "暂时无法保存额度" }, { status: 502 });
  }
}
