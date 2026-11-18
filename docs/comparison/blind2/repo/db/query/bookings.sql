-- name: ActiveHolds :many
SELECT id, sailing_id, seats, created_at, expires_at FROM holds WHERE sailing_id = $1;

-- name: CreateHold :one
INSERT INTO holds (sailing_id, seats, created_at, expires_at) VALUES ($1, $2, $3, $4) RETURNING id;

-- name: GetBooking :one
SELECT id, sailing_id, seats, status, total_cents, created_at FROM bookings WHERE id = $1;

-- name: CreateBooking :one
INSERT INTO bookings (sailing_id, seats, status, total_cents) VALUES ($1, $2, $3, $4) RETURNING id;

-- name: SetBookingStatus :exec
UPDATE bookings SET status = $2 WHERE id = $1;

-- name: CreateRefund :exec
INSERT INTO refunds (booking_id, amount_cents) VALUES ($1, $2);
