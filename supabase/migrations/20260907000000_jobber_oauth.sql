-- Storage for the one Jobber OAuth grant.
--
-- One row, id 'default'. The refresh token in it is the only long-lived
-- credential in the whole integration, so nothing but the service role may
-- read this table: RLS is on and there are deliberately NO policies, which
-- denies anon and authenticated outright. Edge Functions use the service role
-- key, which bypasses RLS; browsers never touch this table.

create table if not exists public.jobber_oauth (
  id            text        primary key default 'default',
  access_token  text,
  refresh_token text        not null,
  expires_at    timestamptz,
  scope         text,
  account_name  text,
  -- A short lease so two concurrent refreshes cannot rotate the refresh token
  -- out from under each other.
  locked_until  timestamptz not null default 'epoch'::timestamptz,
  updated_at    timestamptz not null default now()
);

comment on table public.jobber_oauth is
  'Jobber OAuth tokens. Service-role only: RLS enabled with no policies.';

alter table public.jobber_oauth enable row level security;

revoke all on public.jobber_oauth from anon, authenticated;
