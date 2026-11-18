"""Parser for CSV files that partner stations drop via SFTP."""
import csv
import datetime as dt

from tidewater.models import Reading


def parse_file(path, station_id):
    out = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            ts = dt.datetime.fromisoformat(row["timestamp"]).replace(tzinfo=dt.timezone.utc)
            out.append(
                Reading(
                    station_id=station_id,
                    observed_at=int(ts.timestamp()),
                    level_mm=int(row["level_mm"]),
                    source="csv_drop",
                )
            )
    return out
