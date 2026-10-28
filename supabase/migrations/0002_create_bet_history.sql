-- Upgrade the existing bet_history table for full bet journaling and CLV.

alter table public.bet_history
  add column if not exists event_id text,
  add column if not exists competition text,
  add column if not exists kickoff_at timestamptz,
  add column if not exists outcome text,
  add column if not exists placed_odds numeric,
  add column if not exists stake_fraction numeric default 0,
  add column if not exists stake_amount numeric,
  add column if not exists currency text default 'EUR',
  add column if not exists profile text default 'balanced',
  add column if not exists closing_odds numeric,
  add column if not exists closing_line_value numeric;

update public.bet_history
set placed_odds = provided_odds
where placed_odds is null and provided_odds is not null;

create index if not exists bet_history_created_at_idx
  on public.bet_history (created_at desc);

create index if not exists bet_history_event_idx
  on public.bet_history (event_id);

alter table public.bet_history enable row level security;

drop policy if exists "Service role manages bet history" on public.bet_history;
create policy "Service role manages bet history"
  on public.bet_history
  for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');
