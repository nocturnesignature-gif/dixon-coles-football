create extension if not exists "pgcrypto";

create table if not exists public.teams (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  league text not null,
  created_at timestamptz not null default now()
);

create table if not exists public.matches (
  id uuid primary key default gen_random_uuid(),
  league text not null,
  home_team text not null references public.teams(name) on delete cascade,
  away_team text not null references public.teams(name) on delete cascade,
  kickoff_at timestamptz not null,
  home_goals int,
  away_goals int,
  status text not null default 'scheduled',
  created_at timestamptz not null default now(),
  unique (league, home_team, away_team, kickoff_at)
);

create table if not exists public.predictions (
  id uuid primary key default gen_random_uuid(),
  match_id uuid not null references public.matches(id) on delete cascade,
  model_version text not null default 'dixon-coles-v1',
  home_win_probability numeric not null,
  draw_probability numeric not null,
  away_win_probability numeric not null,
  over_2_5_probability numeric not null,
  btts_probability numeric not null,
  expected_home_goals numeric not null,
  expected_away_goals numeric not null,
  top_scores jsonb not null,
  created_at timestamptz not null default now()
);

create index if not exists matches_kickoff_idx on public.matches (kickoff_at);
create index if not exists predictions_match_idx on public.predictions (match_id);
