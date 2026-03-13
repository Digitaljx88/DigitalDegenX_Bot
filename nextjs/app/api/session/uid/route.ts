import { NextRequest, NextResponse } from "next/server";

const ACTIVE_UID_COOKIE = "ddx_active_uid";
const COOKIE_MAX_AGE = 60 * 60 * 24 * 30;

function normalizeUid(value: unknown): string {
  return String(value ?? "").replace(/[^\d]/g, "").trim();
}

function allowedUid(): string {
  return normalizeUid(process.env.DASHBOARD_ALLOWED_UID || "");
}

export async function GET(request: NextRequest) {
  const uid = normalizeUid(request.cookies.get(ACTIVE_UID_COOKIE)?.value || "");
  return NextResponse.json({ uid: uid ? Number(uid) : null });
}

export async function POST(request: NextRequest) {
  let payload: { uid?: number | string } = {};
  try {
    payload = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  const uid = normalizeUid(payload.uid);
  if (!uid) {
    return NextResponse.json({ error: "UID is required" }, { status: 400 });
  }

  const forcedUid = allowedUid();
  if (forcedUid && uid != forcedUid) {
    return NextResponse.json({ error: "UID is not allowed for this dashboard" }, { status: 403 });
  }

  const response = NextResponse.json({ uid: Number(uid) });
  response.cookies.set({
    name: ACTIVE_UID_COOKIE,
    value: uid,
    httpOnly: true,
    sameSite: "lax",
    secure: true,
    path: "/",
    maxAge: COOKIE_MAX_AGE,
  });
  return response;
}

export async function DELETE() {
  const response = NextResponse.json({ ok: true });
  response.cookies.set({
    name: ACTIVE_UID_COOKIE,
    value: "",
    httpOnly: true,
    sameSite: "lax",
    secure: true,
    path: "/",
    maxAge: 0,
  });
  return response;
}
