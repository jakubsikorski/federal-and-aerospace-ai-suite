-- Schema per docs/design/APPLICATION_DESIGN.md section 7.
-- Stream-scoped by construction (D13): every history table leads with stream_id.
-- There is deliberately no `sessions` table; rows carry their own scoped identity.

CREATE TABLE IF NOT EXISTS streams (
  stream_id       TEXT PRIMARY KEY,
  display_name    TEXT NOT NULL,
  source_uri      TEXT NOT NULL,
  annotated_path  TEXT NOT NULL,
  enabled         INTEGER NOT NULL DEFAULT 1,
  sort_order      INTEGER NOT NULL DEFAULT 0,
  tracking_session_id TEXT,
  history_generation  TEXT,
  dlsps_instance_id   TEXT,
  clear_state     TEXT NOT NULL DEFAULT 'IDLE');

CREATE TABLE IF NOT EXISTS tracks (
  track_id            INTEGER PRIMARY KEY,
  stream_id           TEXT NOT NULL,
  tracking_session_id TEXT NOT NULL,
  history_generation  TEXT NOT NULL,
  tracker_id          INTEGER NOT NULL,
  class_label         TEXT NOT NULL,
  class_id            INTEGER,
  first_observed_utc  TEXT NOT NULL,
  last_observed_utc   TEXT NOT NULL,
  observation_count   INTEGER NOT NULL DEFAULT 0,
  peak_confidence     REAL,
  confidence_freshness TEXT NOT NULL,
  closed_at           TEXT,
  closure_reason      TEXT,
  selected_candidate_id TEXT,
  source_frame_id_first INTEGER,
  frame_sequence_first  INTEGER);

CREATE UNIQUE INDEX IF NOT EXISTS ux_tracks_identity ON tracks
  (stream_id, tracking_session_id, history_generation, tracker_id);
CREATE INDEX IF NOT EXISTS ix_tracks_window  ON tracks (stream_id, last_observed_utc, first_observed_utc);
CREATE INDEX IF NOT EXISTS ix_tracks_class   ON tracks (stream_id, class_label, last_observed_utc);
CREATE INDEX IF NOT EXISTS ix_tracks_current ON tracks
  (stream_id, tracking_session_id, history_generation, closed_at, last_observed_utc);

CREATE TABLE IF NOT EXISTS track_intervals (
  track_id INTEGER NOT NULL,
  interval_index INTEGER NOT NULL,
  stream_id TEXT NOT NULL,
  started_utc TEXT NOT NULL,
  ended_utc TEXT NOT NULL,
  observation_count INTEGER NOT NULL,
  PRIMARY KEY (track_id, interval_index));
CREATE INDEX IF NOT EXISTS ix_intervals_time ON track_intervals (stream_id, started_utc, ended_utc);

CREATE TABLE IF NOT EXISTS candidates (
  candidate_id TEXT PRIMARY KEY,
  track_id INTEGER,
  stream_id TEXT NOT NULL,
  tracking_session_id TEXT NOT NULL,
  history_generation TEXT NOT NULL,
  tracker_id INTEGER NOT NULL,
  region_id INTEGER,
  frame_sequence INTEGER,
  source_frame_id INTEGER,
  observed_at_utc TEXT NOT NULL,
  label TEXT,
  confidence REAL,
  confidence_freshness TEXT,
  detection_rect TEXT,
  crop_rect TEXT,
  frame_width INTEGER, frame_height INTEGER,
  image_width INTEGER, image_height INTEGER,
  quality_score REAL, sharpness REAL,
  object_touches_frame INTEGER, aspect_limited INTEGER,
  storage_key TEXT NOT NULL,
  candidate_number INTEGER,
  expires_at_utc TEXT NOT NULL,
  image_state TEXT NOT NULL DEFAULT 'present');
CREATE INDEX IF NOT EXISTS ix_cand_track ON candidates (track_id, quality_score DESC);
CREATE INDEX IF NOT EXISTS ix_cand_identity ON candidates
  (stream_id, tracking_session_id, history_generation, tracker_id, observed_at_utc);
CREATE INDEX IF NOT EXISTS ix_cand_expiry ON candidates (stream_id, expires_at_utc);

CREATE TABLE IF NOT EXISTS feed_health (
  stream_id TEXT NOT NULL,
  started_utc TEXT NOT NULL,
  ended_utc TEXT,
  ok INTEGER NOT NULL,
  reason TEXT,
  observed_frames INTEGER,
  untracked_detections INTEGER,
  PRIMARY KEY (stream_id, started_utc));
CREATE INDEX IF NOT EXISTS ix_health_time ON feed_health (started_utc, ended_utc);

CREATE TABLE IF NOT EXISTS rules (
  rule_id TEXT PRIMARY KEY, name TEXT, class_label TEXT,
  stream_id TEXT,
  min_confidence REAL, enabled INTEGER, created_utc TEXT,
  updated_utc TEXT, spec_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS rule_matches (
  match_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL,
  track_id INTEGER NOT NULL, matched_utc TEXT NOT NULL,
  dispatch_state TEXT NOT NULL, attempts INTEGER DEFAULT 0,
  last_attempt_utc TEXT, last_error TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_match_logical ON rule_matches (rule_id, track_id);

CREATE TABLE IF NOT EXISTS mission_sources (
  filename TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL, uploaded_utc TEXT,
  status TEXT NOT NULL, corpus_version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS mission_brief (
  brief_id INTEGER PRIMARY KEY, corpus_version INTEGER NOT NULL,
  generated_utc TEXT NOT NULL, brief_json TEXT NOT NULL,
  stale INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE VIEW IF NOT EXISTS q_tracks AS
SELECT t.track_id, t.stream_id, t.tracking_session_id, t.history_generation,
       t.class_label, t.class_id, t.first_observed_utc, t.last_observed_utc,
       t.observation_count, t.peak_confidence, t.confidence_freshness,
       t.closed_at, t.closure_reason,
       EXISTS (SELECT 1 FROM candidates c
               WHERE c.track_id = t.track_id AND c.image_state = 'present') AS has_image,
       (SELECT c.image_state FROM candidates c
        WHERE c.track_id = t.track_id
        ORDER BY (c.image_state = 'present') DESC, c.quality_score DESC LIMIT 1) AS snip_state,
       (SELECT count(*) FROM candidates c
        WHERE c.track_id = t.track_id AND c.image_state = 'present') AS candidate_count
FROM tracks t;

CREATE VIEW IF NOT EXISTS q_intervals AS
SELECT track_id, stream_id, started_utc, ended_utc, observation_count
FROM track_intervals;

-- Classification observations belong to the same track/session as detections.
-- Identical consecutive label sets share an interval; missing metadata ends it.
CREATE TABLE IF NOT EXISTS attribute_samples (
    sample_id INTEGER PRIMARY KEY,
    track_id INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
    started_utc TEXT NOT NULL,
    ended_utc TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    source_json TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_attributes_track_time
    ON attribute_samples(track_id, started_utc, ended_utc);
CREATE INDEX IF NOT EXISTS ix_attributes_time ON attribute_samples(ended_utc);
