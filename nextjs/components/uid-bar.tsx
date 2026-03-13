"use client";

import { useEffect, useState, useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

const STORAGE_KEY = "digitaldegenx_uid";

export function UidBar() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [isPending, startTransition] = useTransition();
  const currentUid = searchParams.get("uid") || "";
  const [draftUid, setDraftUid] = useState("");

  useEffect(() => {
    if (currentUid) {
      localStorage.setItem(STORAGE_KEY, currentUid);
      return;
    }
    const savedUid = localStorage.getItem(STORAGE_KEY);
    if (savedUid) {
      const next = new URLSearchParams(searchParams.toString());
      next.set("uid", savedUid);
      startTransition(() => {
        router.replace(`${pathname}?${next.toString()}`);
      });
    }
  }, [currentUid, pathname, router, searchParams]);

  function applyUid() {
    const next = new URLSearchParams(searchParams.toString());
    const value = draftUid.trim();
    if (value) {
      next.set("uid", value);
      localStorage.setItem(STORAGE_KEY, value);
    } else {
      return;
    }
    startTransition(() => {
      const qs = next.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname);
    });
  }

  function clearUid() {
    const next = new URLSearchParams(searchParams.toString());
    next.delete("uid");
    localStorage.removeItem(STORAGE_KEY);
    setDraftUid("");
    startTransition(() => {
      const qs = next.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname);
    });
  }

  return (
    <div className="flex flex-col gap-3 rounded-2xl border border-white/10 bg-black/15 p-4 md:flex-row md:items-end md:justify-between">
      <div>
        <div className="text-xs uppercase tracking-[0.24em] text-[var(--muted-foreground)]">Active Telegram User</div>
        <div className="mt-1 text-sm text-white">
          {searchParams.get("uid") ? `UID ${searchParams.get("uid")}` : "Set your Telegram user ID to unlock portfolio, trades, and controls."}
        </div>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          value={draftUid || currentUid}
          onChange={(event) => setDraftUid(event.target.value.replace(/[^\d]/g, ""))}
          placeholder="Telegram UID"
          className="rounded-full border border-white/10 bg-black/25 px-4 py-2 text-sm text-white outline-none placeholder:text-[var(--muted-foreground)]"
        />
        <button
          type="button"
          onClick={applyUid}
          disabled={isPending}
          className="rounded-full bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-foreground)] disabled:opacity-60"
        >
          {isPending ? "Saving..." : "Use UID"}
        </button>
        <button
          type="button"
          onClick={clearUid}
          className="rounded-full border border-white/10 px-4 py-2 text-sm text-[var(--muted-foreground)]"
        >
          Clear
        </button>
      </div>
    </div>
  );
}
