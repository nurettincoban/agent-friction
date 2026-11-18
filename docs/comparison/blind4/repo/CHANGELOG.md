# Changelog

All notable changes to kilnctl and kiln-hub. The format follows Keep a Changelog.

## [Unreleased]

## [0.3.2] - 2026-09-24
### Fixed
- `kilnctl plan --start` accepts `HH:MM` again (regression in 0.3.1).

## [0.3.1] - 2026-09-10
### Added
- kiln-hub: `GET /cones/{name}`.
- `kilnctl cone <name> --fahrenheit`.

### Changed
- kiln-hub moved to axum 0.8.

## [0.3.0] - 2026-08-19
### Added
- kiln-hub service with firing log (sqlite).
