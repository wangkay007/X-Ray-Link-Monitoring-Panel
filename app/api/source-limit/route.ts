import { isAuthenticated } from "@/app/lib/auth";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  if (!(await isAuthenticated())) return Response.json({ error: "Unauthorized" }, { status: 401 });
  const endpoint = process.env.MONITOR_ENDPOINT?.replace(/\/v1\/snapshot$/, "/v1/source-limit");
  const token = process.env.MONITOR_TOKEN;
  if (!endpoint || !token) return Response.json({ error: "监控数据源尚未配置" }, { status: 503 });
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: await request.text(),
      cache: "no-store",
    });
    return new Response(await response.text(), {
      status: response.status,
      headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
    });
  } catch {
    return Response.json({ error: "无法确认保存结果，请刷新后查看当前限制" }, { status: 502 });
  }
}
