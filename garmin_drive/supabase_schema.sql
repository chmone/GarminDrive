-- Body Compass Supabase schema (the GarminDrive SQL sink target).
--
-- This is the same DDL the sink runs itself (see sql_sink.SCHEMA_SQL, `create table if not exists`),
-- provided here so you can apply/review it in the Supabase SQL editor. Multi-tenant by `user_id`;
-- every table keeps the full normalized row in a `raw` jsonb column so new/rare source fields never
-- require a migration. Idempotent: re-applying is a no-op.
--
-- RLS is intentionally NOT enabled yet (single trusted writer + the app's trusted-header user seam).
-- When real auth lands, enable RLS and add per-user policies; the cron writer should use a role that
-- bypasses RLS (service_role or a dedicated owner).

create table if not exists health (
  user_id text not null,
  date date not null,
  resting_hr double precision, avg_hr double precision,
  min_hr double precision, max_hr double precision,
  avg_stress double precision, max_stress double precision,
  body_battery_start double precision, body_battery_end double precision,
  body_battery_min double precision, body_battery_max double precision,
  sleep_duration_hours double precision, sleep_score double precision,
  hrv_avg double precision, hrv_status text,
  respiration_avg double precision, spo2_avg double precision,
  training_readiness_score double precision,
  available_metrics text, metric_errors text, fetched_at text,
  raw jsonb,
  primary key (user_id, date)
);

create table if not exists runs (
  user_id text not null,
  source_activity_id text not null,
  local_date date, sport_type text,
  distance_miles double precision, distance_kilometers double precision,
  moving_time_seconds double precision, elapsed_time_seconds double precision,
  pace_seconds_per_mile double precision,
  average_heartrate double precision, max_heartrate double precision, average_cadence double precision,
  elevation_gain_feet double precision, elevation_gain_meters double precision,
  average_speed_mps double precision, max_speed_mps double precision,
  calories double precision, route_available boolean, mile_split_count integer,
  name text, timezone text, start_date_local text, source text,
  strava_activity_url text, route_geojson_path text, raw_data_path text,
  raw jsonb,
  primary key (user_id, source_activity_id)
);

create table if not exists splits (
  user_id text not null,
  source_activity_id text not null,
  split_index integer not null,
  local_date date, split_type text, source text,
  distance_miles double precision, moving_time text, pace_per_mile text,
  average_heartrate double precision, max_heartrate double precision,
  elevation_gain_feet double precision, elevation_loss_feet double precision,
  net_elevation_change_feet double precision, average_cadence double precision,
  average_grade double precision, route_available boolean,
  name text, strava_activity_url text,
  raw jsonb,
  primary key (user_id, source_activity_id, split_index)
);

create table if not exists routes (
  user_id text not null,
  source_activity_id text not null,
  local_date date, sport_type text, name text, source text,
  distance_miles double precision, start_date text, start_date_local text,
  start_lat double precision, start_lon double precision,
  geometry jsonb not null,
  primary key (user_id, source_activity_id)
);

create table if not exists ingest_meta (
  user_id text not null, source text not null,   -- 'strava' | 'garmin_health'
  last_ingested_at timestamptz default now(), row_count integer,
  primary key (user_id, source)
);
