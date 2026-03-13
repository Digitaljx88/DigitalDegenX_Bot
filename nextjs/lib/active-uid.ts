"use client";

import { useCallback, useEffect, useState } from "react";

type SessionUidResponse = {
  uid: number | null;
  error?: string;
};

async function readUid(): Promise<number | null> {
  const response = await fetch("/api/session/uid", {
    method: "GET",
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${body}`);
  }
  const data = (await response.json()) as SessionUidResponse;
  return data.uid ?? null;
}

export function useActiveUid() {
  const [uid, setUidState] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const nextUid = await readUid();
      setUidState(nextUid);
      setError("");
      return nextUid;
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to read dashboard UID";
      setError(message);
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setUid = useCallback(async (value: string | number) => {
    const normalized = String(value ?? "").replace(/[^\d]/g, "").trim();
    if (!normalized) {
      throw new Error("UID is required");
    }
    const response = await fetch("/api/session/uid", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ uid: normalized }),
    });
    if (!response.ok) {
      const body = await response.text();
      throw new Error(`${response.status} ${response.statusText}: ${body}`);
    }
    const data = (await response.json()) as SessionUidResponse;
    setUidState(data.uid ?? null);
    setError("");
    return data.uid ?? null;
  }, []);

  const clearUid = useCallback(async () => {
    const response = await fetch("/api/session/uid", {
      method: "DELETE",
    });
    if (!response.ok) {
      const body = await response.text();
      throw new Error(`${response.status} ${response.statusText}: ${body}`);
    }
    setUidState(null);
    setError("");
  }, []);

  return {
    uid,
    loading,
    error,
    refresh,
    setUid,
    clearUid,
  };
}
