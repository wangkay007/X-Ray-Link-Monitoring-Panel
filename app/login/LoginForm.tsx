"use client";

import { FormEvent, useState } from "react";

export default function LoginForm() {
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "登录失败");
      window.location.assign("/");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
      setSubmitting(false);
    }
  }

  return (
    <form className="login-form" onSubmit={submit}>
      <label>
        <span>用户名</span>
        <input name="username" type="text" autoComplete="username" required autoFocus />
      </label>
      <label>
        <span>密码</span>
        <input name="password" type="password" autoComplete="current-password" required />
      </label>
      {error ? <p className="login-error" role="alert">{error}</p> : null}
      <button type="submit" disabled={submitting}>{submitting ? "正在验证…" : "登录监控台"}</button>
    </form>
  );
}
