-- Expose forecast protection metadata in the experimental forecasts view.
DROP VIEW public.football_experimental_forecasts_v4;
CREATE VIEW public.football_experimental_forecasts_v4 WITH (security_invoker=true) AS
SELECT
  f.id AS forecast_id,
  e.id AS event_id,
  e.external_id,
  e.competition,
  e.home_team,
  e.away_team,
  e.kickoff_at,
  f.predicted_at,
  f.recorded_at,
  f.input_cutoff_at,
  f.home_probability,
  f.draw_probability,
  f.away_probability,
  f.eligibility,
  (f.features->>'experimental')::boolean AS experimental,
  (f.recorded_at IS NOT NULL) AS protected_forecast,
  m.model_version,
  m.status AS model_status
FROM public.football_forecasts_v4 f
JOIN public.football_events_v4 e ON e.id = f.event_id
JOIN public.football_model_runs_v4 m ON m.id = f.model_run_id
WHERE f.features->>'experimental' = 'true';

REVOKE ALL ON public.football_experimental_forecasts_v4 FROM anon, authenticated;
GRANT SELECT ON public.football_experimental_forecasts_v4 TO service_role;
