import { isAuthenticated } from "@/app/lib/auth";

export const dynamic = "force-dynamic";

export async function GET() {
  if (!(await isAuthenticated())) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const endpoint = process.env.MONITOR_ENDPOINT;
  const token = process.env.MONITOR_TOKEN;
  if (!endpoint || !token) {
    return Response.json(
      { error: "监控数据源尚未配置" },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }

  try {
    const response = await fetch(endpoint, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (!response.ok) {
      return Response.json(
        { error: `数据源返回 ${response.status}` },
        { status: 502, headers: { "Cache-Control": "no-store" } },
      );
    }
    const body = await response.text();
    return new Response(body, {
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
      },
    });
  } catch {
    return Response.json(
      { error: "暂时无法连接监控数据源" },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
}
