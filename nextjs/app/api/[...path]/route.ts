import { NextRequest } from "next/server";

const ACTIVE_UID_COOKIE = "ddx_active_uid";
const BOT_API_BASE_URL =
  process.env.BOT_API_BASE_URL ||
  process.env.NEXT_PUBLIC_BOT_API_BASE_URL ||
  "http://127.0.0.1:8080";

const BOT_API_KEY =
  process.env.BOT_API_KEY ||
  process.env.API_KEY ||
  process.env.NEXT_PUBLIC_BOT_API_KEY ||
  "";

function normalizeUid(value: unknown): string {
  return String(value ?? "").replace(/[^\d]/g, "").trim();
}

function routeNeedsUid(path: string[]): boolean {
  const head = path[0] || "";
  if (head === "portfolio" || head === "trades" || head === "history" || head === "research-log" || head === "buy" || head === "sell" || head === "mode") {
    return true;
  }
  if (head === "autobuy" || head === "settings" || head === "autosell" || head === "trade-controls" || head === "sniper" || head === "wallet") {
    return true;
  }
  if (head === "scanner" && ["feed", "threshold", "top", "watchlist"].includes(path[1] || "")) {
    return true;
  }
  if (head === "token" && path[2] === "snapshot") {
    return true;
  }
  if (head === "message") {
    return true;
  }
  return false;
}

function rewritePathUid(path: string[], uid: string): string[] {
  if (!uid) return path;
  if (path[0] === "autobuy" && path[1] === "activity" && path[2]) {
    return [path[0], path[1], uid, ...path.slice(3)];
  }
  // Sniper: /api/sniper/status → /sniper/{uid}/status  (uid injected after prefix)
  if (path[0] === "sniper" && path[1]) {
    return [path[0], uid, ...path.slice(1)];
  }
  // Wallet tracked: /api/wallet/tracked → /wallet/tracked/{uid}
  if (path[0] === "wallet" && path[1] === "tracked") {
    return [path[0], path[1], uid, ...path.slice(2)];
  }
  if ((path[0] === "autobuy" || path[0] === "settings" || path[0] === "trade-controls") && path[1]) {
    return [path[0], uid, ...path.slice(2)];
  }
  return path;
}

async function forward(request: NextRequest, path: string[]) {
  const activeUid = normalizeUid(request.cookies.get(ACTIVE_UID_COOKIE)?.value || "");
  if (routeNeedsUid(path) && !activeUid) {
    return new Response("Dashboard UID is not set", { status: 401 });
  }

  const rewrittenPath = rewritePathUid(path, activeUid);
  const target = new URL(rewrittenPath.join("/"), BOT_API_BASE_URL.endsWith("/") ? BOT_API_BASE_URL : `${BOT_API_BASE_URL}/`);
  request.nextUrl.searchParams.forEach((value, key) => {
    if (key === "uid" && activeUid) {
      return;
    }
    target.searchParams.set(key, value);
  });
  if (
    routeNeedsUid(path) &&
    activeUid &&
    !target.searchParams.get("uid") &&
    path[0] !== "autobuy" &&
    path[0] !== "settings" &&
    path[0] !== "trade-controls"
  ) {
    target.searchParams.set("uid", activeUid);
  }

  const headers = new Headers();
  headers.set("Content-Type", request.headers.get("content-type") || "application/json");
  if (BOT_API_KEY) {
    headers.set("X-API-Key", BOT_API_KEY);
  }

  const init: RequestInit = {
    method: request.method,
    headers,
    cache: "no-store",
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    const rawBody = await request.text();
    if (rawBody && headers.get("Content-Type")?.includes("application/json")) {
      try {
        const payload = JSON.parse(rawBody);
        if (routeNeedsUid(path) && activeUid) {
          payload.uid = Number(activeUid);
        }
        init.body = JSON.stringify(payload);
      } catch {
        init.body = rawBody;
      }
    } else {
      init.body = rawBody;
    }
  }

  // Retry once on connection refused — the bot may be mid-restart
  let response: Response;
  try {
    response = await fetch(target, init);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes("ECONNREFUSED") || msg.includes("fetch failed")) {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        response = await fetch(target, init);
      } catch {
        return new Response(JSON.stringify({ error: "Bot API unavailable — retrying shortly" }), {
          status: 503,
          headers: { "content-type": "application/json" },
        });
      }
    } else {
      throw err;
    }
  }
  const body = await response.text();
  return new Response(body, {
    status: response.status,
    headers: {
      "content-type": response.headers.get("content-type") || "application/json",
    },
  });
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  return forward(request, path);
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  return forward(request, path);
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  return forward(request, path);
}
