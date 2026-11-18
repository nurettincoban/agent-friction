CREATE TABLE kilns (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    max_c   INTEGER NOT NULL
);
