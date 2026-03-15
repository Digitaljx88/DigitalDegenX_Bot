"use client";
import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";

export function ApiStatusDot() {
  const [online, setOnline] = useState<boolean | null>(null);

  useEffect(() => {
    async function check() {
      try {
        await apiFetch("/scanner/feed", { query: { limit: 1 } });
        setOnline(true);
      } catch {
        setOnline(false);
      }
    }
    check();
    const t = setInterval(check, 30_000);
    return () => clearInterval(t);
  }, []);

  if (online === null) return <span className="h-2 w-2 rounded-full bg-white/20" />;
  return (
    <span
      className={`h-2 w-2 rounded-full ${online ? "live-dot bg-emerald-400" : "bg-red-400"}`}
      title={online ? "API online" : "API offline"}
    />
  );
}
