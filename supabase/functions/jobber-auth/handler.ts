// Jobber OAuth, held in Supabase instead of on a laptop.
//
// Why this exists: Jobber needs a redirect URI it can send a browser to, and
// localhost is not one when the person consenting is not sitting at the
// machine that will pull the data. This function is that URI. It also means
// the Jobber client secret and the refresh token live in exactly one place --
// Supabase secrets and a service-role-only table -- and the data pipeline
// never holds either. It asks for a short-lived access token instead.
//
// This function is deployed with verify_jwt = false, because the redirect
// Jobber sends cannot carry a Supabase JWT. That makes it PUBLIC, so it
// authenticates every caller itself:
//
//   GET  /jobber-auth/start?key=...   admin key -- begins the consent flow
//   GET  /jobber-auth/callback        accepts only state this function signed
//   POST /jobber-auth/token           token key -- returns an access token
//   GET  /jobber-auth/health          no secrets, reports what is configured
//
// Required secrets (set with: supabase secrets set NAME=value):
//   JOBBER_CLIENT_ID, JOBBER_CLIENT_SECRET, JOBBER_REDIRECT_URI,
//   MVH_ADMIN_KEY, MVH_TOKEN_KEY, MVH_STATE_SECRET
// SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are injected by the platform.

// Read the environment per request rather than at module load, so a redeploy
// is not needed to pick up a rotated secret -- and so tests can set it.
const env = (name: string, fallback = "") => Deno.env.get(name) ?? fallback;

const authorizeUrl = () =>
  env("JOBBER_AUTHORIZE_URL", "https://api.getjobber.com/api/oauth/authorize");
const tokenUrl = () =>
  env("JOBBER_TOKEN_URL", "https://api.getjobber.com/api/oauth/token");

const clientId = () => env("JOBBER_CLIENT_ID");
const clientSecret = () => env("JOBBER_CLIENT_SECRET");
const redirectUri = () => env("JOBBER_REDIRECT_URI");
const adminKey = () => env("MVH_ADMIN_KEY");
const tokenKey = () => env("MVH_TOKEN_KEY");
const stateSecret = () => env("MVH_STATE_SECRET");

const supabaseUrl = () => env("SUPABASE_URL");
const serviceKey = () => env("SUPABASE_SERVICE_ROLE_KEY");

const ROW_ID = "default";
const TABLE = "jobber_oauth";
const STATE_TTL_MS = 15 * 60 * 1000;   // a consent link is good for 15 minutes
const EXPIRY_MARGIN_MS = 5 * 60 * 1000; // hand back a token with life left in it
const LOCK_SECONDS = 30;
const UNLOCKED = new Date(0).toISOString();

// ---------------------------------------------------------------- utilities
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function page(title: string, detail: string, status = 200): Response {
  const esc = (text: string) =>
    text.replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
  return new Response(
    `<!doctype html><meta charset="utf-8">` +
    `<title>${esc(title)}</title>` +
    `<body style="font-family:system-ui;max-width:34rem;margin:4rem auto;padding:0 1rem">` +
    `<h2>${esc(title)}</h2><p style="color:#444">${esc(detail)}</p></body>`,
    { status, headers: { "content-type": "text/html; charset=utf-8" } },
  );
}

/** Constant-time compare, so a wrong key cannot be found one byte at a time. */
export function secretsMatch(a: string, b: string): boolean {
  if (!a || !b) return false;
  const left = new TextEncoder().encode(a);
  const right = new TextEncoder().encode(b);
  let diff = left.length ^ right.length;          // no early return on length
  const max = Math.max(left.length, right.length);
  for (let i = 0; i < max; i++) diff |= (left[i] ?? 0) ^ (right[i] ?? 0);
  return diff === 0;
}

function b64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function unb64url(text: string): Uint8Array {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/")
    .padEnd(Math.ceil(text.length / 4) * 4, "=");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

async function hmac(message: string): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(stateSecret()),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  return new Uint8Array(
    await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message)));
}

/** State is self-validating: a signed payload, so nothing has to be stored
 *  between the redirect out and the redirect back. */
export async function signState(): Promise<string> {
  const payload = b64url(new TextEncoder().encode(
    JSON.stringify({ t: Date.now(), n: crypto.randomUUID() })));
  return `${payload}.${b64url(await hmac(payload))}`;
}

export async function stateIsValid(state: string): Promise<boolean> {
  const [payload, signature] = (state ?? "").split(".");
  if (!payload || !signature) return false;
  if (!secretsMatch(signature, b64url(await hmac(payload)))) return false;
  try {
    const { t } = JSON.parse(new TextDecoder().decode(unb64url(payload)));
    return typeof t === "number" && Date.now() - t < STATE_TTL_MS;
  } catch {
    return false;
  }
}

// ------------------------------------------------------------------- storage
type Row = {
  access_token: string | null;
  refresh_token: string;
  expires_at: string | null;
  scope: string | null;
  locked_until: string;
};

const restHeaders = () => ({
  "apikey": serviceKey(),
  "authorization": `Bearer ${serviceKey()}`,
  "content-type": "application/json",
});

async function readRow(): Promise<Row | null> {
  const select = "access_token,refresh_token,expires_at,scope,locked_until";
  const resp = await fetch(
    `${supabaseUrl()}/rest/v1/${TABLE}?id=eq.${ROW_ID}&select=${select}`,
    { headers: restHeaders() });
  if (!resp.ok) throw new Error(`reading tokens failed: ${await resp.text()}`);
  return (await resp.json())[0] ?? null;
}

async function writeRow(fields: Record<string, unknown>): Promise<void> {
  const resp = await fetch(`${supabaseUrl()}/rest/v1/${TABLE}`, {
    method: "POST",
    headers: { ...restHeaders(), "prefer": "resolution=merge-duplicates" },
    body: JSON.stringify({
      id: ROW_ID, updated_at: new Date().toISOString(), ...fields,
    }),
  });
  if (!resp.ok) throw new Error(`saving tokens failed: ${await resp.text()}`);
}

/** Take a short lease before refreshing. Jobber may rotate the refresh token,
 *  so two workers refreshing at once would invalidate each other's. */
async function takeLock(): Promise<boolean> {
  const until = new Date(Date.now() + LOCK_SECONDS * 1000).toISOString();
  const resp = await fetch(
    `${supabaseUrl()}/rest/v1/${TABLE}` +
    `?id=eq.${ROW_ID}&locked_until=lt.${new Date().toISOString()}`,
    {
      method: "PATCH",
      headers: { ...restHeaders(), "prefer": "return=representation" },
      body: JSON.stringify({ locked_until: until }),
    });
  if (!resp.ok) throw new Error(`locking failed: ${await resp.text()}`);
  return (await resp.json()).length > 0;
}

// --------------------------------------------------------------------- jobber
async function callTokenEndpoint(form: Record<string, string>) {
  const resp = await fetch(tokenUrl(), {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(form),
  });
  const text = await resp.text();
  if (!resp.ok) {
    throw new Error(`Jobber returned HTTP ${resp.status}: ${text.slice(0, 300)}`);
  }
  return JSON.parse(text);
}

async function storeGrant(body: Record<string, unknown>, previousRefresh?: string) {
  const refresh = (body.refresh_token as string) || previousRefresh;
  if (!refresh) throw new Error("Jobber returned no refresh token to store");
  await writeRow({
    access_token: body.access_token,
    // Rotation is on for some apps. Losing the replacement means the next
    // refresh fails and someone has to consent all over again.
    refresh_token: refresh,
    expires_at: new Date(
      Date.now() + Number(body.expires_in ?? 3600) * 1000).toISOString(),
    scope: body.scope ?? null,
    locked_until: UNLOCKED,
  });
}

/** A live access token, refreshing only when the cached one is nearly out. */
async function currentAccessToken(force: boolean) {
  const row = await readRow();
  if (!row) {
    throw new Error("No Jobber grant stored yet. Open " +
      "/jobber-auth/start?key=... in a browser as a Jobber admin.");
  }

  const expiresAt = row.expires_at ? Date.parse(row.expires_at) : 0;
  if (row.access_token && !force && expiresAt - Date.now() > EXPIRY_MARGIN_MS) {
    return {
      access_token: row.access_token, expires_at: row.expires_at,
      scope: row.scope, refreshed: false,
    };
  }

  let current = row;
  if (!await takeLock()) {
    // Another request holds the lease. Give it a moment, then take its result
    // only if it really did produce a live token -- a row that is still expired
    // means that worker has not finished or has failed, and handing back an
    // expired token would just move the failure downstream.
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const fresh = await readRow();
    const freshExpiry = fresh?.expires_at ? Date.parse(fresh.expires_at) : 0;
    if (fresh?.access_token && freshExpiry - Date.now() > EXPIRY_MARGIN_MS) {
      return {
        access_token: fresh.access_token, expires_at: fresh.expires_at,
        scope: fresh.scope, refreshed: false,
      };
    }
    if (fresh) current = fresh;
  }

  let body: Record<string, unknown>;
  try {
    body = await callTokenEndpoint({
      client_id: clientId(),
      client_secret: clientSecret(),
      grant_type: "refresh_token",
      refresh_token: current.refresh_token,
    });
  } catch (exc) {
    // Release the lease so the next attempt is not stuck behind a dead one.
    await writeRow({ locked_until: UNLOCKED }).catch(() => {});
    throw exc;
  }
  await storeGrant(body, current.refresh_token);
  return {
    access_token: body.access_token,
    expires_at: new Date(
      Date.now() + Number(body.expires_in ?? 3600) * 1000).toISOString(),
    scope: body.scope ?? current.scope,
    refreshed: true,
  };
}

// --------------------------------------------------------------------- routes
function missingConfig(): string[] {
  return Object.entries({
    JOBBER_CLIENT_ID: clientId(),
    JOBBER_CLIENT_SECRET: clientSecret(),
    JOBBER_REDIRECT_URI: redirectUri(),
    MVH_ADMIN_KEY: adminKey(),
    MVH_TOKEN_KEY: tokenKey(),
    MVH_STATE_SECRET: stateSecret(),
    SUPABASE_URL: supabaseUrl(),
    SUPABASE_SERVICE_ROLE_KEY: serviceKey(),
  }).filter(([, value]) => !value).map(([name]) => name);
}

async function handleStart(url: URL): Promise<Response> {
  if (!secretsMatch(url.searchParams.get("key") ?? "", adminKey())) {
    return page("Not authorized",
      "This link needs the admin key. Anyone can reach this URL, so consent " +
      "cannot start without it.", 401);
  }
  const authorize = new URL(authorizeUrl());
  authorize.searchParams.set("client_id", clientId());
  authorize.searchParams.set("redirect_uri", redirectUri());
  authorize.searchParams.set("response_type", "code");
  authorize.searchParams.set("state", await signState());
  return new Response(null, {
    status: 302, headers: { location: authorize.toString() },
  });
}

async function handleCallback(url: URL): Promise<Response> {
  const error = url.searchParams.get("error");
  if (error) {
    return page("Jobber refused the authorization",
      url.searchParams.get("error_description") ?? error, 400);
  }
  const code = url.searchParams.get("code") ?? "";
  if (!code) return page("No authorization code", "Jobber sent no code.", 400);
  if (!await stateIsValid(url.searchParams.get("state") ?? "")) {
    // Either not ours, or older than the 15-minute window.
    return page("State check failed",
      "This response did not come from a consent flow this function started, " +
      "or the link has expired. Start again from /start?key=...", 400);
  }

  try {
    await storeGrant(await callTokenEndpoint({
      client_id: clientId(),
      client_secret: clientSecret(),
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri(),
    }));
  } catch (exc) {
    return page("Could not store the grant", String(exc), 500);
  }
  return page("Jobber connected",
    "The refresh token is stored. You can close this tab -- run " +
    "`python -m mvh jobber probe` to confirm the pipeline can reach it.");
}

async function handleToken(request: Request, url: URL): Promise<Response> {
  if (!secretsMatch(request.headers.get("x-mvh-key") ?? "", tokenKey())) {
    return json({ error: "unauthorized" }, 401);
  }
  try {
    return json(await currentAccessToken(url.searchParams.get("force") === "true"));
  } catch (exc) {
    return json({ error: String(exc) }, 502);
  }
}

async function handleHealth(): Promise<Response> {
  const missing = missingConfig();
  let connected = false;
  let detail = "";
  try {
    connected = Boolean((await readRow())?.refresh_token);
  } catch (exc) {
    detail = String(exc);
  }
  const ok = missing.length === 0 && connected;
  return json({
    ok,
    missing_config: missing,          // names only, never values
    jobber_connected: connected,
    detail: detail || undefined,
  }, ok ? 200 : 503);
}

export async function handler(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const route = url.pathname.replace(/^.*\/jobber-auth/, "").replace(/\/$/, "");

  const missing = missingConfig();
  if (missing.length && route !== "/health" && route !== "") {
    return json({ error: `not configured: ${missing.join(", ")}` }, 503);
  }

  if (request.method === "GET" && route === "/start") return handleStart(url);
  if (request.method === "GET" && route === "/callback") return handleCallback(url);
  if (request.method === "POST" && route === "/token") return handleToken(request, url);
  if (request.method === "GET" && (route === "/health" || route === "")) {
    return handleHealth();
  }
  return json({ error: `no route for ${request.method} ${route || "/"}` }, 404);
}
