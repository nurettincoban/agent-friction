"""Write readings to TimescaleDB."""
import tomllib

import psycopg

DEFAULT_URL = "postgresql://tide:tide@localhost:5433/tide"


def load_config(path):
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def connect(url=DEFAULT_URL):
    return psycopg.connect(url, autocommit=True)


def apply_migration(conn, path):
    conn.execute(open(path).read())


def write_readings(conn, readings):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO readings (station_id, observed_at, level_mm, sigma_mm, source, qc_flags)"
            " VALUES (%s, to_timestamp(%s), %s, %s, %s, %s)"
            " ON CONFLICT (station_id, observed_at) DO NOTHING",
            [
                (r.station_id, r.observed_at, r.level_mm, r.sigma_mm, r.source, r.qc_flags)
                for r in readings
            ],
        )
