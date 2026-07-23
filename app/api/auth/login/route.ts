import { createSessionCookie, verifyPassword } from "@/app/lib/auth";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  let payload: { username?: string; password?: string };
  try {
    payload = await request.json();
  } catch {
    return Response.json({ error: "请求格式不正确" }, { status: 400 });
  }

  const username = String(payload.username ?? "").trim();
  const password = String(payload.password ?? "");
  if (!username || !password) {
    return Response.json({ error: "请输入用户名和密码" }, { status: 400 });
  }

  if (!(await verifyPassword(username, password))) {
    await new Promise((resolve) => setTimeout(resolve, 450));
    return Response.json({ error: "用户名或密码不正确" }, { status: 401 });
  }

  return Response.json(
    { ok: true },
    { headers: { "Set-Cookie": await createSessionCookie(), "Cache-Control": "no-store" } },
  );
}
