CREATE TABLE bookings (
    id          bigserial PRIMARY KEY,
    sailing_id  bigint NOT NULL REFERENCES sailings(id),
    seats       integer NOT NULL,
    status      text NOT NULL,
    total_cents bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX bookings_sailing_idx ON bookings (sailing_id);
