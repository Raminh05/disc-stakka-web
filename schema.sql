-- Disc Stakka catalogue.
--
-- Slot occupancy is derived from disc.slot, never stored separately: a slot is
-- taken if a row references it. That holds even while a disc is checked out,
-- because the slot stays reserved so the disc has somewhere to go back to.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS disc (
    id            INTEGER PRIMARY KEY,
    slot          INTEGER NOT NULL UNIQUE,   -- 1..100
    title         TEXT    NOT NULL,
    subtitle      TEXT,                      -- artist, studio, whatever fits
    category      TEXT,                      -- Video games / Music / Movies / Software / Other
    platform      TEXT,                      -- which console, for a game; see catalog/taxonomy.py
    notes         TEXT,
    art_path      TEXT,                      -- basename under static/art
    status        TEXT    NOT NULL DEFAULT 'stored',   -- 'stored' | 'out'
    checked_out_at TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    CHECK (slot BETWEEN 1 AND 100),
    CHECK (status IN ('stored', 'out'))
);

CREATE INDEX IF NOT EXISTS disc_status_idx ON disc (status);
CREATE INDEX IF NOT EXISTS disc_title_idx  ON disc (title);
CREATE INDEX IF NOT EXISTS disc_kind_idx   ON disc (category, platform);

-- Audit trail. Also how physical drift gets reconciled: if the catalogue and
-- the carousel disagree, this is the record of what was believed to happen.
CREATE TABLE IF NOT EXISTS event (
    id      INTEGER PRIMARY KEY,
    disc_id INTEGER REFERENCES disc(id) ON DELETE SET NULL,
    slot    INTEGER,
    kind    TEXT NOT NULL,   -- added|ejected|returned|retracted|manual|removed|failed
    detail  TEXT,
    at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS event_at_idx ON event (at DESC);
