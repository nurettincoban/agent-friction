CREATE TABLE routes (
    code             text PRIMARY KEY,
    name             text NOT NULL,
    adult_fare_cents bigint NOT NULL
);

CREATE TABLE sailings (
    id               bigserial PRIMARY KEY,
    route_code       text NOT NULL REFERENCES routes(code),
    departs_at       timestamptz NOT NULL,
    capacity         integer NOT NULL,
    adult_fare_cents bigint NOT NULL
);
