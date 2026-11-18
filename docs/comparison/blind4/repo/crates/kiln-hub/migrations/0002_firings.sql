CREATE TABLE firings (
    id           INTEGER PRIMARY KEY,
    kiln_id      INTEGER NOT NULL REFERENCES kilns(id),
    preset       TEXT NOT NULL,
    peak_c       INTEGER NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);
