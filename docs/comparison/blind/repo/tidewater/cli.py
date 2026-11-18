import pathlib

import click
import yaml

from tidewater.ingest import csv_drop, noaa
from tidewater.load import warehouse


@click.group()
def main():
    """tidewater pipeline commands."""


@main.group()
def stations():
    pass


@stations.command("list")
@click.option("--config", default="config/stations.yaml")
def stations_list(config):
    data = yaml.safe_load(open(config))
    for s in data["stations"]:
        click.echo(f"{s['id']}\t{s['name']}\t{s['source']}")


@main.command()
@click.option("--dir", "mig_dir", default="migrations")
def migrate(mig_dir):
    conn = warehouse.connect()
    for path in sorted(pathlib.Path(mig_dir).glob("*.sql")):
        warehouse.apply_migration(conn, path)
        click.echo(f"applied {path.name}")


@main.command()
@click.option("--config", default="config/pipeline.toml")
@click.option("--station", multiple=True)
def backfill(config, station):
    cfg = warehouse.load_config(config)
    conn = warehouse.connect(cfg["warehouse"]["url"])
    for sid in station:
        readings = noaa.fetch_range(sid, cfg)
        warehouse.write_readings(conn, readings)


if __name__ == "__main__":
    main()
