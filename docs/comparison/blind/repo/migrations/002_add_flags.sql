CREATE INDEX IF NOT EXISTS readings_flagged_idx ON readings (station_id, observed_at)
    WHERE qc_flags <> 0;
