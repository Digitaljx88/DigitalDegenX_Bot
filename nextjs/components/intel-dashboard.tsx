"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Panel } from "@/components/panel";
import { apiFetch } from "@/lib/api";

type IntelDashboardProps = {
  title: string;
  subtitle: string;
  endpoint: string;
  collectionKey: string;
  secondaryCollectionKey?: string;
  secondaryTitle?: string;
};

const intelLinks = [
  { href: "/intel/wallets", label: "Wallets" },
  { href: "/intel/narratives", label: "Narratives" },
  { href: "/intel/discovery", label: "Discovery" },
  { href: "/intel/cluster", label: "Clusters" },
  { href: "/intel/bundle", label: "Bundles" },
  { href: "/intel/playbook", label: "Playbook" },
];

export function IntelDashboard({
  title,
  subtitle,
  endpoint,
  collectionKey,
  secondaryCollectionKey,
  secondaryTitle = "Recent events",
}: IntelDashboardProps) {
  const pathname = usePathname();
  const [items, setItems] = useState<Record<string, unknown>[]>([]);
  const [secondaryItems, setSecondaryItems] = useState<Record<string, unknown>[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    async function load() {
      try {
        const response = await apiFetch<Record<string, unknown>>(endpoint);
        const rows = Array.isArray(response[collectionKey]) ? (response[collectionKey] as Record<string, unknown>[]) : [];
        const extraRows =
          secondaryCollectionKey && Array.isArray(response[secondaryCollectionKey])
            ? (response[secondaryCollectionKey] as Record<string, unknown>[])
            : [];
        setItems(rows);
        setSecondaryItems(extraRows);
        setError("");
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load intelligence panel");
      }
    }
    void load();
  }, [collectionKey, endpoint, secondaryCollectionKey]);

  return (
    <Panel title={title} subtitle={subtitle}>
      <div className="mb-5 flex flex-wrap gap-3">
        {intelLinks.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={`rounded-full border px-4 py-2 text-xs transition ${
              pathname === item.href
                ? "border-orange-400/50 bg-orange-500/20 text-orange-100"
                : "border-white/10 text-[var(--muted-foreground)] hover:border-white/20 hover:text-white"
            }`}
          >
            {item.label}
          </Link>
        ))}
      </div>
      {error ? <div className="mb-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-100">{error}</div> : null}
      <div className="mb-4 grid gap-3 md:grid-cols-3">
        <SummaryCard label="Rows" value={String(items.length)} />
        <SummaryCard label="Secondary events" value={String(secondaryItems.length)} />
        <SummaryCard label="Endpoint" value={endpoint.replace("/intel/", "")} />
      </div>
      <div className="space-y-3">
        {items.length ? items.map((item, index) => (
          <IntelCard key={index} item={item} />
        )) : <div className="text-sm text-[var(--muted-foreground)]">No data yet.</div>}
      </div>
      {secondaryItems.length ? (
        <div className="mt-6 space-y-3">
          <div className="text-sm font-medium text-white">{secondaryTitle}</div>
          {secondaryItems.map((item, index) => (
            <IntelCard key={`secondary-${index}`} item={item} compact />
          ))}
        </div>
      ) : null}
    </Panel>
  );
}

function SummaryCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-white/8 bg-black/10 p-4">
      <div className="text-xs uppercase tracking-[0.2em] text-[var(--muted-foreground)]">{label}</div>
      <div className="mt-2 text-lg font-semibold text-white break-all">{value}</div>
    </div>
  );
}

function IntelCard({ item, compact = false }: { item: Record<string, unknown>; compact?: boolean }) {
  const entries = Object.entries(item);
  const mint = typeof item.mint === "string" ? item.mint : null;
  const address = typeof item.address === "string" ? item.address : null;
  const title =
    (typeof item.name === "string" && item.name) ||
    (typeof item.symbol === "string" && item.symbol) ||
    address ||
    mint ||
    "Record";

  return (
    <div className="rounded-2xl border border-white/8 bg-black/10 p-4 text-sm">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <div className="font-medium text-white">{title}</div>
          {mint ? <div className="mt-1 text-xs text-[var(--muted-foreground)]">{mint}</div> : null}
          {!mint && address ? <div className="mt-1 text-xs text-[var(--muted-foreground)]">{address}</div> : null}
        </div>
        {mint ? (
          <Link
            href={`/token/${mint}`}
            className="rounded-full border border-white/10 px-3 py-1.5 text-xs text-[var(--muted-foreground)] transition hover:border-white/20 hover:text-white"
          >
            View token
          </Link>
        ) : null}
      </div>
      <div className="grid gap-2 md:grid-cols-2">
        {entries
          .filter(([key]) => !["mint", "address", "name", "symbol"].includes(key))
          .slice(0, compact ? 6 : 10)
          .map(([key, value]) => (
            <div key={key} className="rounded-xl border border-white/6 bg-black/10 px-3 py-2">
              <div className="text-[10px] uppercase tracking-[0.18em] text-[var(--muted-foreground)]">
                {key.replace(/_/g, " ")}
              </div>
              <div className="mt-1 text-white break-all">
                {formatIntelValue(value)}
              </div>
            </div>
          ))}
      </div>
    </div>
  );
}

function formatIntelValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "n/a";
  if (typeof value === "number") {
    if (Number.isInteger(value)) return value.toLocaleString();
    return value.toFixed(2);
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) return value.map((item) => (typeof item === "object" ? JSON.stringify(item) : String(item))).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
