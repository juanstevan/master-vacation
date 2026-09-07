// Entry point. All the logic lives in handler.ts so it can be exercised by
// tests (tests/test_supabase_function.ts) without starting a server.
//
// See handler.ts for the routes, the secrets this needs, and why the function
// authenticates its own callers.
import { handler } from "./handler.ts";

Deno.serve(handler);
