// Tests for the Jobber OAuth Edge Function.  bun tests/test_supabase_function.ts
//
// Runs handler.ts directly with a shimmed Deno.env, an in-memory stand-in for
// PostgREST, and a fake Jobber token endpoint -- so the consent flow, the key
// checks and the refresh path are all exercised without deploying anything or
// touching a real Jobber account.
//
// Everything here is INVENTED TEST DATA. No real credentials.

const SUPABASE_URL = "http://supabase.test";
const TOKEN_URL = "http://jobber.test/api/oauth/token";
const AUTHORIZE_URL = "http://jobber.test/api/oauth/authorize";

const config: Record<string, string> = {
  JOBBER_CLIENT_ID: "test-client-id",
  JOBBER_CLIENT_SECRET: "test-client-secret",
  JOBBER_REDIRECT_URI: "https://proj.supabase.co/functions/v1/jobber-auth/callback",
  JOBBER_AUTHORIZE_URL: AUTHORIZE_URL,
  JOBBER_TOKEN_URL: TOKEN_URL,
  MVH_ADMIN_KEY: "admin-key-aaaaaaaa",
  MVH_TOKEN_KEY: "token-key-bbbbbbbb",
  MVH_STATE_SECRET: "state-secret-cccccccc",
  SUPABASE_URL,
  SUPABASE_SERVICE_ROLE_KEY: "service-role-key",
};

(globalThis as Record<string, unknown>).Deno = {
  env: { get: (name: string) => config[name] },
};

// ------------------------------------------------- in-memory PostgREST + Jobber
type Row = Record<string, unknown>;
let table: Row | null = null;
const jobber = {
  refreshToken: "refresh-1",
  rotate: true,
  calls: 0,
  lastForm: {} as Record<string, string>,
  failNext: false,
};

function resetState() {
  table = null;
  jobber.refreshToken = "refresh-1";
  jobber.rotate = true;
  jobber.calls = 0;
  jobber.lastForm = {};
  jobber.failNext = false;
}

const realFetch = globalThis.fetch;
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === "string" ? input : input.toString());
  const method = (init?.method ?? "GET").toUpperCase();

  if (url.origin === new URL(SUPABASE_URL).origin) {
    if (method === "GET") {
      return Response.json(table ? [table] : []);
    }
    if (method === "POST") {                       // upsert
      table = { ...(table ?? {}), ...JSON.parse(String(init?.body)) };
      return new Response("", { status: 201 });
    }
    if (method === "PATCH") {                      // conditional lock take
      const guard = url.searchParams.get("locked_until") ?? "";
      const cutoff = guard.startsWith("lt.") ? Date.parse(guard.slice(3)) : 0;
      const held = Date.parse(String(table?.locked_until ?? "1970-01-01")) || 0;
      if (!table || !(held < cutoff)) return Response.json([]);
      table = { ...table, ...JSON.parse(String(init?.body)) };
      return Response.json([table]);
    }
  }

  if (url.origin === new URL(TOKEN_URL).origin) {
    jobber.calls++;
    const form = Object.fromEntries(new URLSearchParams(String(init?.body)));
    jobber.lastForm = form as Record<string, string>;
    if (jobber.failNext) {
      jobber.failNext = false;
      return new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 });
    }
    if (form.grant_type === "authorization_code" && form.code !== "good-code") {
      return new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 });
    }
    if (form.grant_type === "refresh_token" && form.refresh_token !== jobber.refreshToken) {
      return new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 });
    }
    const body: Record<string, unknown> = {
      access_token: `access-${jobber.calls}`, expires_in: 3600,
      scope: "read_jobs read_clients",
    };
    if (jobber.rotate) {
      jobber.refreshToken = `refresh-${jobber.calls + 1}`;
      body.refresh_token = jobber.refreshToken;
    }
    return Response.json(body);
  }

  return realFetch(input, init);
}) as typeof fetch;

const { handler, secretsMatch } = await import(
  "../supabase/functions/jobber-auth/handler.ts");

// ------------------------------------------------------------------- helpers
const BASE = "https://proj.supabase.co/functions/v1/jobber-auth";
const call = (path: string, init?: RequestInit) =>
  handler(new Request(`${BASE}${path}`, init));

async function connect() {
  const started = await call(`/start?key=${config.MVH_ADMIN_KEY}`);
  const state = new URL(started.headers.get("location")!).searchParams.get("state")!;
  return call(`/callback?code=good-code&state=${encodeURIComponent(state)}`);
}

let failures = 0;
async function test(name: string, body: () => Promise<void> | void) {
  resetState();
  try {
    await body();
    console.log(`  PASS  ${name}`);
  } catch (exc) {
    failures++;
    console.log(`  FAIL  ${name}: ${exc instanceof Error ? exc.message : exc}`);
  }
}
function assert(condition: unknown, message: string) {
  if (!condition) throw new Error(message);
}

// --------------------------------------------------------------------- tests
await test("constant-time compare still compares correctly", () => {
  assert(secretsMatch("abc", "abc"), "equal strings should match");
  assert(!secretsMatch("abc", "abd"), "different strings must not match");
  assert(!secretsMatch("abc", "abcd"), "different lengths must not match");
  assert(!secretsMatch("", ""), "empty secrets must never match");
});

await test("start refuses without the admin key", async () => {
  assert((await call("/start")).status === 401, "no key should be 401");
  assert((await call("/start?key=wrong")).status === 401, "wrong key should be 401");
});

await test("start redirects to Jobber with a signed state", async () => {
  const resp = await call(`/start?key=${config.MVH_ADMIN_KEY}`);
  assert(resp.status === 302, `expected 302, got ${resp.status}`);
  const target = new URL(resp.headers.get("location")!);
  assert(target.origin + target.pathname === AUTHORIZE_URL, target.toString());
  assert(target.searchParams.get("client_id") === config.JOBBER_CLIENT_ID, "client_id");
  assert(target.searchParams.get("redirect_uri") === config.JOBBER_REDIRECT_URI,
    "redirect_uri must match what is registered on the app");
  assert(target.searchParams.get("response_type") === "code", "response_type");
  assert((target.searchParams.get("state") ?? "").includes("."), "state is unsigned");
});

await test("callback rejects a state it did not sign", async () => {
  const resp = await call("/callback?code=good-code&state=forged.signature");
  assert(resp.status === 400, `expected 400, got ${resp.status}`);
  assert(table === null, "nothing should have been stored");
});

await test("callback rejects a tampered payload", async () => {
  const started = await call(`/start?key=${config.MVH_ADMIN_KEY}`);
  const state = new URL(started.headers.get("location")!).searchParams.get("state")!;
  const [, signature] = state.split(".");
  const forged = `${btoa('{"t":0,"n":"x"}').replace(/=+$/, "")}.${signature}`;
  const resp = await call(`/callback?code=good-code&state=${encodeURIComponent(forged)}`);
  assert(resp.status === 400, `expected 400, got ${resp.status}`);
});

await test("callback exchanges the code and stores the grant", async () => {
  const resp = await connect();
  assert(resp.status === 200, `expected 200, got ${resp.status}`);
  assert(table !== null, "no row stored");
  assert(table!.refresh_token === jobber.refreshToken, "stored a stale refresh token");
  assert(String(table!.access_token).startsWith("access-"), "no access token stored");
  assert(jobber.lastForm.grant_type === "authorization_code", "wrong grant type");
  assert(jobber.lastForm.redirect_uri === config.JOBBER_REDIRECT_URI,
    "redirect_uri must be echoed on the exchange");
});

await test("callback surfaces a refusal from Jobber", async () => {
  const started = await call(`/start?key=${config.MVH_ADMIN_KEY}`);
  const state = new URL(started.headers.get("location")!).searchParams.get("state")!;
  const resp = await call(`/callback?code=bad-code&state=${encodeURIComponent(state)}`);
  assert(resp.status === 500, `expected 500, got ${resp.status}`);
  assert(table === null, "a failed exchange must not store anything");
});

await test("callback reports an error Jobber redirected with", async () => {
  const resp = await call("/callback?error=access_denied&error_description=Nope");
  assert(resp.status === 400, `expected 400, got ${resp.status}`);
  assert((await resp.text()).includes("Nope"), "should show Jobber's reason");
});

await test("token endpoint refuses without the token key", async () => {
  await connect();
  assert((await call("/token", { method: "POST" })).status === 401, "no key");
  const wrong = await call("/token", {
    method: "POST", headers: { "x-mvh-key": "nope" },
  });
  assert(wrong.status === 401, "wrong key should be 401");
});

await test("token endpoint returns the cached access token", async () => {
  await connect();
  const before = jobber.calls;
  const resp = await call("/token", {
    method: "POST", headers: { "x-mvh-key": config.MVH_TOKEN_KEY },
  });
  assert(resp.status === 200, `expected 200, got ${resp.status}`);
  const body = await resp.json();
  assert(body.access_token === table!.access_token, "returned a different token");
  assert(body.refreshed === false, "should not refresh a token with an hour left");
  assert(jobber.calls === before, "should not have called Jobber at all");
});

await test("token endpoint refreshes an expired token and keeps the rotation", async () => {
  await connect();
  table = { ...table!, expires_at: new Date(Date.now() - 1000).toISOString() };
  const resp = await call("/token", {
    method: "POST", headers: { "x-mvh-key": config.MVH_TOKEN_KEY },
  });
  const body = await resp.json();
  assert(resp.status === 200, `expected 200, got ${resp.status}`);
  assert(body.refreshed === true, "an expired token should have been refreshed");
  assert(table!.refresh_token === jobber.refreshToken,
    "the rotated refresh token was not persisted -- the next run would fail");
  assert(Date.parse(String(table!.locked_until)) === 0, "the lock was not released");
});

await test("force refreshes even while the cached token is valid", async () => {
  await connect();
  const before = jobber.calls;
  const resp = await call("/token?force=true", {
    method: "POST", headers: { "x-mvh-key": config.MVH_TOKEN_KEY },
  });
  assert((await resp.json()).refreshed === true, "force should refresh");
  assert(jobber.calls === before + 1, "force should call Jobber exactly once");
});

await test("a dead refresh token gives a 502 with the reason", async () => {
  await connect();
  table = { ...table!, expires_at: new Date(Date.now() - 1000).toISOString() };
  jobber.failNext = true;
  const resp = await call("/token", {
    method: "POST", headers: { "x-mvh-key": config.MVH_TOKEN_KEY },
  });
  assert(resp.status === 502, `expected 502, got ${resp.status}`);
  assert((await resp.json()).error.includes("invalid_grant"), "should say why");
});

await test("token endpoint explains when nothing has been connected yet", async () => {
  const resp = await call("/token", {
    method: "POST", headers: { "x-mvh-key": config.MVH_TOKEN_KEY },
  });
  assert(resp.status === 502, `expected 502, got ${resp.status}`);
  assert((await resp.json()).error.includes("/jobber-auth/start"),
    "should point at the consent flow");
});

await test("health reports missing configuration by name only", async () => {
  const saved = config.JOBBER_CLIENT_SECRET;
  delete (config as Record<string, string | undefined>).JOBBER_CLIENT_SECRET;
  try {
    const resp = await call("/health");
    assert(resp.status === 503, `expected 503, got ${resp.status}`);
    const body = await resp.json();
    assert(body.missing_config.includes("JOBBER_CLIENT_SECRET"), "should name it");
    const text = JSON.stringify(body);
    for (const secret of [saved, config.MVH_ADMIN_KEY, config.MVH_TOKEN_KEY,
                          config.MVH_STATE_SECRET, config.SUPABASE_SERVICE_ROLE_KEY]) {
      assert(!text.includes(secret), "health leaked a secret value");
    }
  } finally {
    config.JOBBER_CLIENT_SECRET = saved;
  }
});

await test("health is green once connected", async () => {
  await connect();
  const resp = await call("/health");
  assert(resp.status === 200, `expected 200, got ${resp.status}`);
  const body = await resp.json();
  assert(body.ok === true && body.jobber_connected === true, JSON.stringify(body));
});

await test("unknown routes 404 instead of doing something surprising", async () => {
  await connect();
  assert((await call("/nope")).status === 404, "unknown path");
  const wrongMethod = await call("/token");     // GET, not POST
  assert(wrongMethod.status === 404, "GET /token should not be a token grant");
});

console.log(`\n${failures === 0 ? "all" : ""} ${failures} failure(s)`);
process.exit(failures ? 1 : 0);
