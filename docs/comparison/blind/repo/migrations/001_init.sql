CREATE TABLE IF NOT EXISTS readings (
    station_id  text        NOT NULL,
    observed_at timestamptz NOT NULL,
    level_mm    integer     NOT NULL,
    sigma_mm    integer,
    source      text        NOT NULL,
    qc_flags    integer     NOT NULL DEFAULT 0,
    PRIMARY KEY (station_id, observed_at)
);
SELECT create_hypertable('readings', 'observed_at', if_not_exists => TRUE);
