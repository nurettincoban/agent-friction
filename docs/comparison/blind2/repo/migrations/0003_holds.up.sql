CREATE TABLE holds (
    id          bigserial PRIMARY KEY,
    sailing_id  bigint NOT NULL REFERENCES sailings(id),
    seats       integer NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);
