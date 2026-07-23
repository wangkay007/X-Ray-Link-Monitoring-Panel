import { cookies } from "next/headers";

const COOKIE_NAME = "dog_cloud_session";
const SESSION_SECONDS = 7 * 24 * 60 * 60;

function toHex(bytes: ArrayBuffer) {
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function hmac(value: string) {
  const secret = process.env.SESSION_SECRET;
  if (!secret) throw new Error("SESSION_SECRET is not configured");
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return toHex(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value)));
}

export async function verifyPassword(username: string, password: string) {
  const expectedUsername = process.env.ADMIN_USERNAME;
  const expectedPassword = process.env.ADMIN_PASSWORD;
  if (!expectedUsername || !expectedPassword || username !== expectedUsername) return false;
  if (password.length !== expectedPassword.length) return false;
  let difference = 0;
  for (let index = 0; index < password.length; index += 1) {
    difference |= password.charCodeAt(index) ^ expectedPassword.charCodeAt(index);
  }
  return difference === 0;
}

export async function createSessionCookie() {
  const expires = Math.floor(Date.now() / 1000) + SESSION_SECONDS;
  const payload = String(expires);
  const signature = await hmac(payload);
  return `${COOKIE_NAME}=${payload}.${signature}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_SECONDS}`;
}

export function clearSessionCookie() {
  return `${COOKIE_NAME}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0`;
}

export async function isAuthenticated() {
  const store = await cookies();
  const value = store.get(COOKIE_NAME)?.value;
  if (!value) return false;
  const [payload, signature] = value.split(".", 2);
  const expires = Number(payload);
  if (!payload || !signature || !Number.isFinite(expires) || expires < Date.now() / 1000) return false;
  try {
    return (await hmac(payload)) === signature;
  } catch {
    return false;
  }
}
