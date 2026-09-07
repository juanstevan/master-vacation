// Pipedream workflow: hand a short-lived Jobber access token to the pipeline.
//
// This is the whole integration on the Pipedream side. Pipedream owns the
// Jobber OAuth app, so there is no redirect URI to register, no Jobber
// developer app to create, no client secret, and no refresh token to store or
// rotate -- connecting the account in the Pipedream UI covers all of it.
//
// The machine that runs `python -m mvh jobber pull` holds nothing but the
// shared key below. If it is ever compromised it leaks read access until you
// change one environment variable here.
//
// SETUP (see pipedream/README.md for the click-by-click version):
//   1. New workflow, trigger "New Requests (HTTP / Webhook)".
//   2. On the trigger, set HTTP Response to
//      "Return a custom response from your workflow".
//   3. Add a Node.js code step and paste this file into it.
//   4. In the step's props, connect your Jobber account.
//   5. Settings -> Environment Variables: add MVH_TOKEN_KEY (any long random
//      string; `openssl rand -hex 32` is a good way to make one).
//   6. Deploy, then copy the trigger's URL.

export default defineComponent({
  props: {
    // Renders the "Connect Jobber" button. Pipedream keeps the tokens fresh.
    jobber: {
      type: "app",
      app: "jobber",
    },
  },

  async run({ steps, $ }) {
    // This URL is public, so the shared key is the only thing standing in
    // front of it. Compare in constant time: a plain === leaks how much of a
    // guess was right through how long the comparison takes.
    const supplied = steps.trigger.event.headers?.["x-mvh-key"] ?? "";
    const expected = process.env.MVH_TOKEN_KEY ?? "";

    if (!expected) {
      return $.respond({
        status: 500,
        headers: { "content-type": "application/json" },
        body: {
          error: "MVH_TOKEN_KEY is not set on this workflow. Add it under " +
                 "Settings -> Environment Variables, then redeploy.",
        },
      });
    }

    if (!constantTimeEqual(supplied, expected)) {
      return $.respond({
        status: 401,
        headers: { "content-type": "application/json" },
        body: { error: "unauthorized" },
      });
    }

    const accessToken = this.jobber?.$auth?.oauth_access_token;
    if (!accessToken) {
      return $.respond({
        status: 502,
        headers: { "content-type": "application/json" },
        body: {
          error: "No Jobber account is connected to this step. Open the " +
                 "workflow, click the Jobber prop, connect the account, and " +
                 "redeploy.",
        },
      });
    }

    // Pipedream refreshes the token behind the scenes, so every invocation
    // hands back a live one. Reporting a short life keeps the pipeline asking
    // again well inside Jobber's 60-minute expiry rather than trusting a
    // number we cannot actually see.
    const expiresAt = new Date(Date.now() + 30 * 60 * 1000).toISOString();

    return $.respond({
      status: 200,
      headers: { "content-type": "application/json" },
      body: {
        access_token: accessToken,
        expires_at: expiresAt,
        // Only ever the scopes, never the token, so workflow logs stay clean.
        scope: this.jobber?.$auth?.scope ?? "",
      },
    });
  },
});

/** Length-independent comparison, so a wrong key cannot be discovered one
 *  character at a time by timing the responses. */
function constantTimeEqual(a, b) {
  if (!a || !b) return false;
  let diff = a.length ^ b.length;
  const max = Math.max(a.length, b.length);
  for (let i = 0; i < max; i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}
