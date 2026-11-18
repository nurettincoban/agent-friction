CREATE TABLE refunds (
    id           bigserial PRIMARY KEY,
    booking_id   bigint NOT NULL REFERENCES bookings(id),
    amount_cents bigint NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
