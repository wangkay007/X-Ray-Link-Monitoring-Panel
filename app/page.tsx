import type { Metadata } from "next";
import { redirect } from "next/navigation";
import Dashboard from "./Dashboard";
import { isAuthenticated } from "./lib/auth";

export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "狗云监控",
  description: "个人 Xray 流量与来源 IP 监控台",
};

export default async function Home() {
  if (!(await isAuthenticated())) redirect("/login");
  return <Dashboard />;
}
