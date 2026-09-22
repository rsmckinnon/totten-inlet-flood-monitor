BEGIN;
CREATE TABLE IF NOT EXISTS local_tide_observations (
    id UUID PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    reading_inches NUMERIC(9,4) NOT NULL CHECK (reading_inches BETWEEN -1200 AND 1200),
    note TEXT NOT NULL DEFAULT '' CHECK (char_length(note) <= 2000),
    datum TEXT NOT NULL DEFAULT 'property_flood_entry_threshold_zero_inches_v1'
        CHECK (datum = 'property_flood_entry_threshold_zero_inches_v1')
);
CREATE INDEX IF NOT EXISTS local_tide_observations_recent
    ON local_tide_observations (observed_at DESC, submitted_at DESC, id DESC);
COMMIT;
