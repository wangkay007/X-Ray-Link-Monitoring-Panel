import { redirect } from "next/navigation";
import { isAuthenticated } from "@/app/lib/auth";
import LoginForm from "./LoginForm";

export const dynamic = "force-dynamic";

export default async function LoginPage() {
  if (await isAuthenticated()) redirect("/");
  return (
    <main className="login-shell">
      <section className="login-panel">
        <div className="login-brand">
          <span className="brand-mark" aria-hidden="true">D</span>
          <div><strong>狗云监控</strong><span>个人 Xray 控制台</span></div>
        </div>
        <div className="login-copy">
          <h1>登录后查看服务器状态</h1>
          <p>流量、来源 IP 与链接额度属于私有数据，请先完成身份验证。</p>
        </div>
        <LoginForm />
        <p className="login-footnote">登录状态将在 7 天后自动失效</p>
      </section>
    </main>
  );
}
