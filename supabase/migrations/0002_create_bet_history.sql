-- Bet journal with closing-line value tracking.

create table if not exists public.bet_history (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  event_id text,
  competition text,
  home_team text not null,
  away_team text not null,
  kickoff_at timestamptz,
  market text not null,
  outcome text not null,
  model_probability numeric not null check (model_probability >= 0 and model_probability <= 1),
  placed_odds numeric not null check (placed_odds > 1),
  expected_value numeric not null,
  stake_fraction numeric not null check (stake_fraction >= 0),
  stake_amount numeric,
  currency text not null default 'EUR',
  profile text not null default 'balanced',
  closing_odds numeric check (closing_odds is null or closing_odds > 1),
  closing_line_value numeric,
  result text check (result is null or result in ('pending','won','lost','void')),
  profit numeric,
  settled_at timestamptz
);

create index if not exists bet_history_created_at_idx
  on public.bet_history (created_at desc);

create index if not exists bet_history_event_idx
  on public.bet_history (event_id);

alter table public.bet_history enable row level security;

-- The application uses the service-role key server-side. No public anon access.
drop policy if exists "Service role manages bet history" on public.bet_history;
create policy "Service role manages bet history"
  on public.bet_history
  for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');
