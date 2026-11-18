# kilnlog

Plan firings, look up cones, and keep a log of what went into the kiln.

```
cargo kc plan --preset glaze6 --start 07:30
cargo kc cone 6 --fahrenheit
```

## kiln-hub

```
just db-setup   # needs sqlx-cli: cargo install sqlx-cli --no-default-features --features sqlite
just hub
```

The hub listens on `KILNHUB_BIND` (default 127.0.0.1:8087).

## License

MIT
