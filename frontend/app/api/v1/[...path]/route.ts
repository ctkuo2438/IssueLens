import { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type Context = { params: Promise<{ path: string[] }> };

async function proxy(request: NextRequest, context: Context) {
  const { path } = await context.params;
  const origin = process.env.BACKEND_URL || "http://127.0.0.1:8000";
  const url = `${origin}/api/v1/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  try {
    const headers = new Headers();
    for (const name of ["content-type", "last-event-id", "cookie", "origin"]) {
      const value = request.headers.get(name);
      if (value) headers.set(name, value);
    }
    const response = await fetch(url, {
      method: request.method,
      headers,
      body: request.method === "POST" ? await request.text() : undefined,
      cache: "no-store",
      redirect: "manual",
      signal: request.signal,
    });
    const responseHeaders = new Headers({
      "Cache-Control": "private, no-store, no-transform",
      "X-Accel-Buffering": "no",
    });
    for (const name of ["content-type", "referrer-policy"]) {
      const value = response.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    const location = response.headers.get("location");
    if (location) {
      responseHeaders.set("Location", location.startsWith(`${origin}/`) ? location.slice(origin.length) : location);
    }
    for (const cookie of response.headers.getSetCookie()) responseHeaders.append("Set-Cookie", cookie);
    return new Response(response.body, {
      status: response.status,
      headers: responseHeaders,
    });
  } catch {
    return Response.json(
      {
        detail: "Cannot reach the API. Start the FastAPI server on port 8000.",
      },
      { status: 502 },
    );
  }
}

export { proxy as GET, proxy as POST, proxy as DELETE };
