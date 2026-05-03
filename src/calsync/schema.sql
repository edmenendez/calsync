-- calsync SQLite schema. Idempotent: every CREATE uses IF NOT EXISTS.
-- Run via calsync.db.init_db(path).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  label TEXT UNIQUE NOT NULL,
  email TEXT NOT NULL,
  google_calendar_id TEXT,                    -- resolved primary calendar ID; populated on first OAuth
  refresh_token_encrypted BLOB NOT NULL,
  access_token TEXT,
  access_token_expires_at TIMESTAMP,
  needs_reauth BOOLEAN NOT NULL DEFAULT 0,
  paused BOOLEAN NOT NULL DEFAULT 0,
  last_error TEXT,
  last_error_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS calendars (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  google_calendar_id TEXT NOT NULL,
  sync_token TEXT,
  last_sync_at TIMESTAMP,
  UNIQUE(account_id, google_calendar_id)
);

CREATE TABLE IF NOT EXISTS watch_channels (
  id INTEGER PRIMARY KEY,
  calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
  channel_id TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  channel_token TEXT NOT NULL,
  callback_url TEXT NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  stopped_at TIMESTAMP
);

-- Partial unique index: at most one ACTIVE (not stopped) channel per calendar.
-- Renewal flow: (1) register new on Google, (2) in a single transaction
-- UPDATE old SET stopped_at = now THEN INSERT new, (3) best-effort stop on Google.
CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_channels_one_active_per_calendar
  ON watch_channels(calendar_id) WHERE stopped_at IS NULL;

CREATE TABLE IF NOT EXISTS event_links (
  id INTEGER PRIMARY KEY,
  link_id TEXT UNIQUE NOT NULL,
  mirror_key TEXT NOT NULL,
  source_calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
  source_event_id TEXT NOT NULL,
  mirror_calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
  mirror_event_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('full', 'busy')),
  source_start_at TIMESTAMP NOT NULL,
  source_end_at TIMESTAMP NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_calendar_id, source_event_id, mirror_calendar_id)
);

CREATE INDEX IF NOT EXISTS idx_event_links_source
  ON event_links(source_calendar_id, source_event_id);
CREATE INDEX IF NOT EXISTS idx_event_links_mirror
  ON event_links(mirror_calendar_id, mirror_event_id);
CREATE INDEX IF NOT EXISTS idx_event_links_mirror_key
  ON event_links(mirror_key);
CREATE INDEX IF NOT EXISTS idx_event_links_window
  ON event_links(source_start_at);

CREATE TABLE IF NOT EXISTS admin_log (
  id INTEGER PRIMARY KEY,
  ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  endpoint TEXT NOT NULL,
  method TEXT NOT NULL,
  payload_sha256 TEXT,
  source_ip TEXT,
  result_code INTEGER,
  notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_admin_log_ts ON admin_log(ts DESC);
