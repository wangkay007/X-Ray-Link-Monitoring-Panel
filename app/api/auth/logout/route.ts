import { clearSessionCookie } from "@/app/lib/auth";

export async function POST() {
  return Response.json(
    { ok: true },
    { headers: { "Set-Cookie": clearSessionCookie(), "Cache-Control": "no-store" } },
  );
}
