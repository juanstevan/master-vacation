// Tests for the Pipedream workflow.  bun tests/test_pipedream_workflow.js
//
// Pipedream has no local runner, so this shims the two things it injects --
// defineComponent and $.respond -- and calls the step directly. That is enough
// to prove the key check, the error paths and the response shape the pipeline
// depends on.
//
// Everything here is INVENTED TEST DATA. No real credentials.

globalThis.defineComponent = (component) => component;
const module = await import("../pipedream/jobber-token.js");
const component = module.default;

/** Run the step the way Pipedream would, with a connected account or not. */
async function invoke({ key, token = "jobber-access-token-abc", scope = "read_jobs",
                        connected = true, envKey = "the-shared-key" } = {}) {
  process.env.MVH_TOKEN_KEY = envKey;
  let response = null;
  const context = {
    jobber: connected ? { $auth: { oauth_access_token: token, scope } } : {},
  };
  const steps = {
    trigger: { event: { headers: key === undefined ? {} : { "x-mvh-key": key } } },
  };
  const $ = { respond: (r) => { response = r; return r; } };
  await component.run.call(context, { steps, $ });
  return response;
}

let failures = 0;
async function test(name, body) {
  try {
    await body();
    console.log(`  PASS  ${name}`);
  } catch (exc) {
    failures++;
    console.log(`  FAIL  ${name}: ${exc.message}`);
  }
}
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};

await test("returns the access token in the shape the pipeline expects", async () => {
  const resp = await invoke({ key: "the-shared-key" });
  assert(resp.status === 200, `expected 200, got ${resp.status}`);
  assert(resp.body.access_token === "jobber-access-token-abc", "wrong token");
  assert(resp.body.scope === "read_jobs", "scope missing");
  // mvh/jobber/remote_auth.py parses this to decide when to ask again.
  const expiry = Date.parse(resp.body.expires_at);
  assert(!Number.isNaN(expiry), "expires_at is not a parseable timestamp");
  const minutes = (expiry - Date.now()) / 60000;
  assert(minutes > 5 && minutes < 60,
    `expiry of ${minutes} min should sit inside Jobber's 60-minute window`);
});

await test("refuses a request with no key", async () => {
  const resp = await invoke({ key: undefined });
  assert(resp.status === 401, `expected 401, got ${resp.status}`);
  assert(!JSON.stringify(resp.body).includes("jobber-access-token"),
    "a refused request must not leak the token");
});

await test("refuses a wrong key", async () => {
  assert((await invoke({ key: "wrong" })).status === 401, "wrong key");
  assert((await invoke({ key: "" })).status === 401, "empty key");
  assert((await invoke({ key: "the-shared-ke" })).status === 401, "prefix of the key");
  assert((await invoke({ key: "the-shared-keyy" })).status === 401, "key plus a char");
});

await test("says so when the workflow has no key configured", async () => {
  const resp = await invoke({ key: "anything", envKey: "" });
  assert(resp.status === 500, `expected 500, got ${resp.status}`);
  assert(resp.body.error.includes("MVH_TOKEN_KEY"), "should name the variable");
  assert(resp.body.error.includes("Environment Variables"),
    "should say where to set it");
});

await test("says so when no Jobber account is connected", async () => {
  const resp = await invoke({ key: "the-shared-key", connected: false });
  assert(resp.status === 502, `expected 502, got ${resp.status}`);
  assert(resp.body.error.includes("connect"), "should say what to do");
});

await test("an unconfigured workflow is refused before the key is compared", async () => {
  // Otherwise an empty MVH_TOKEN_KEY would make an empty header a valid key.
  const resp = await invoke({ key: "", envKey: "" });
  assert(resp.status === 500, `expected 500, got ${resp.status}`);
});

console.log(`\n${failures} failure(s)`);
process.exit(failures ? 1 : 0);
