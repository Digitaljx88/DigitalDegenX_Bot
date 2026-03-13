import { NextRequest } from "next/server";

const BOT_API_BASE_URL =
  process.env.BOT_API_BASE_URL ||
  process.env.NEXT_PUBLIC_BOT_API_BASE_URL ||
  "http://127.0.0.1:8080";

const BOT_API_KEY =
  process.env.BOT_API_KEY ||
  process.env.NEXT_PUBLIC_BOT_API_KEY ||
  "";

async function forward(request: NextRequest, path: string[]) {
  const target = new URL(path.join("/"), BOT_API_BASE_URL.endsWith("/") ? BOT_API_BASE_URL : `${BOT_API_BASE_URL}/`);
  request.nextUrl.searchParams.forEach((value, key) => {
    target.searchParams.set(key, value);
  });

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
    init.body = await request.text();
  }

  const response = await fetch(target, init);
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
