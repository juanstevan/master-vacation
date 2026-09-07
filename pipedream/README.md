# Connecting Jobber through Pipedream

This is the simplest way to get the pipeline talking to Jobber, and it is the
recommended one.

Pipedream owns the Jobber OAuth app. That means **you do not create a Jobber
developer app, you do not register a redirect URI, and there is no client
secret or refresh token for anyone to store**. You click a button, sign into
Jobber, and Pipedream keeps the tokens fresh from then on.

The machine that runs the pull ends up holding exactly one secret: a shared key
you invent, which lets it ask Pipedream for an access token.

## How the pieces fit

```
your laptop / server                  Pipedream                     Jobber
--------------------                  ---------                     ------
python -m mvh jobber pull
   |
   |  POST <workflow URL>
   |  header: x-mvh-key                  checks the key
   |------------------------------->     reads the token it     <-- keeps it
   |                                     already keeps fresh        refreshed
   |  { "access_token": "..." }
   |<-------------------------------
   |
   |  GraphQL, straight to Jobber, using that token
   |----------------------------------------------------------------->
```

Only the token crosses to your machine, and only for about half an hour.

## Setup, step by step

**1. Make a shared key.** Any long random string. On a Mac or Linux terminal:

```bash
openssl rand -hex 32
```

Copy the output somewhere safe for a minute — you need it twice.

**2. Create the workflow.** In Pipedream: **New Workflow**. For the trigger,
choose **New Requests (HTTP / Webhook)**.

**3. Turn on custom responses.** Still on the trigger, find **HTTP Response**
and set it to **Return a custom response from your workflow**. This is easy to
miss, and without it the workflow returns Pipedream's own "success" message
instead of the token, and the pipeline has nothing to read.

**4. Add the code.** Click **+** under the trigger, choose **Node.js**, and
replace the whole placeholder with the contents of `jobber-token.js` from this
folder.

**5. Connect Jobber.** The step now shows a **Jobber** field with a *Connect*
button. Click it, sign into Jobber in the popup, and approve. This is the only
moment anyone touches a Jobber login, and it must be someone who can approve
access for the account.

**6. Add the key.** Go to **Settings → Environment Variables** and add:

| Name | Value |
|---|---|
| `MVH_TOKEN_KEY` | the random string from step 1 |

**7. Deploy** the workflow, then copy the trigger's URL. It looks like
`https://eoXXXXXXXXXXXX.m.pipedream.net`.

## Point the pipeline at it

On the machine that will pull the data:

```bash
export JOBBER_TOKEN_ENDPOINT=https://eoXXXXXXXXXXXX.m.pipedream.net
export JOBBER_TOKEN_KEY=<the same random string from step 1>

python -m mvh jobber probe
```

`probe` should report `auth mode remote` and then work through the chain —
token, API version, jobs scope — telling you exactly which step failed if one
does. When it is happy:

```bash
python -m mvh jobber pull --max-pages 2   # smoke test first
python -m mvh jobber pull --months 24     # the real backfill
```

## If something is wrong

| What you see | What it means |
|---|---|
| `401` / "rejected the key" | `JOBBER_TOKEN_KEY` and `MVH_TOKEN_KEY` differ |
| `502` / "No Jobber account is connected" | step 5 was skipped, or the workflow was not redeployed after connecting |
| `500` / "MVH_TOKEN_KEY is not set" | step 6 was skipped, or it was added after the last deploy |
| The response is not JSON | step 3 was skipped — the trigger is not set to return a custom response |
| `probe` fails on scopes | see the note below |

**A note on scopes.** Pipedream's Jobber app requests its own set of
permissions. If `probe` reports that the `jobs` query is not visible, that app
does not grant jobs read access for your account, and the fix is to use your
own Jobber developer app instead — Pipedream lists it separately as **Jobber
(Developer App)**, which asks you for a client ID and secret. Everything else
in this guide stays the same. There is no way to know which applies until the
account is connected and `probe` runs.

## Testing

```bash
bun tests/test_pipedream_workflow.js
```

Pipedream has no local runner, so the test shims the two things it injects
(`defineComponent` and `$.respond`) and calls the step directly — enough to
cover the key check, both failure messages and the response shape the pipeline
reads.
