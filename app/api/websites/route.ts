import { isAuthenticated } from "@/app/lib/auth";

export const dynamic = "force-dynamic";

function collectorUrl(request: Request) {
  const endpoint = process.env.MONITOR_ENDPOINT?.replace(/\/v1\/snapshot$/, "/v1/websites");
  if (!endpoint) return null;
  const incoming = new URL(request.url);
  return endpoint + incoming.search;
}

async function proxy(request: Request, method: "GET" | "DELETE") {
  if (!(await isAuthenticated())) return Response.json({ error: "Unauthorized" }, { status: 401 });
  const endpoint = collectorUrl(request);
  const token = process.env.MONITOR_TOKEN;
  if (!endpoint || !token) return Response.json({ error: "监控数据源尚未配置" }, { status: 503 });
  try {
    const response = await fetch(endpoint, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(method === "DELETE" ? { "Content-Type": "application/json" } : {}),
      },
      ...(method === "DELETE" ? { body: await request.text() } : {}),
      cache: "no-store",
    });
    return new Response(await response.text(), {
      status: response.status,
      headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
    });
  } catch {
    return Response.json({ error: "暂时无法查询访问网站记录" }, { status: 502 });
  }
}

export async function GET(request: Request) { return proxy(request, "GET"); }
export async function DELETE(request: Request) { return proxy(request, "DELETE"); }
