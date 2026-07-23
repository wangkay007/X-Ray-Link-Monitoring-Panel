import { isAuthenticated } from "@/app/lib/auth";

export const dynamic = "force-dynamic";

async function proxy(request: Request, method: "POST" | "PATCH" | "DELETE") {
  if (!(await isAuthenticated())) return Response.json({ error: "Unauthorized" }, { status: 401 });
  const endpoint = process.env.MONITOR_ENDPOINT?.replace(/\/v1\/snapshot$/, "/v1/links");
  const token = process.env.MONITOR_TOKEN;
  if (!endpoint || !token) return Response.json({ error: "监控数据源尚未配置" }, { status: 503 });
  try {
    const response = await fetch(endpoint, {
      method,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: await request.text(),
      cache: "no-store",
    });
    return new Response(await response.text(), {
      status: response.status,
      headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
    });
  } catch {
    return Response.json({ error: "暂时无法管理链接" }, { status: 502 });
  }
}

export async function POST(request: Request) { return proxy(request, "POST"); }
export async function PATCH(request: Request) { return proxy(request, "PATCH"); }
export async function DELETE(request: Request) { return proxy(request, "DELETE"); }
