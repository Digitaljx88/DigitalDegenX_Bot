"use client";

import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";

type IntelDashboardProps = {
  title: string;
  subtitle: string;
  endpoint: string;
  collectionKey: string;
};

export function IntelDashboard({ title, subtitle, endpoint, collectionKey }: IntelDashboardProps) {
  const [items, setItems] = useState<Record<string, unknown>[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      try {
        const response = await apiFetch<Record<string, unknown>>(endpoint);
        const rows = Array.isArray(response[collectionKey]) ? (response[collectionKey] as Record<string, unknown>[]) : [];
        setItems(rows);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load intelligence panel");
      }
    }
    void load();
  }, [collectionKey, endpoint]);

  return (
    <Panel title={title} subtitle={subtitle}>
      {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
      <div className="space-y-3">
        {items.length ? items.map((item, index) => (
          <div key={index} className="rounded-2xl border border-white/8 bg-black/10 p-4 text-sm">
            {Object.entries(item).slice(0, 6).map(([key, value]) => (
              <div key={key} className="grid grid-cols-[140px_1fr] gap-3 py-1">
                <div className="text-[var(--muted-foreground)]">{key}</div>
                <div className="text-white">{typeof value === "object" ? JSON.stringify(value) : String(value)}</div>
              </div>
            ))}
          </div>
        )) : <div className="text-sm text-[var(--muted-foreground)]">No data yet.</div>}
      </div>
    </Panel>
  );
}
