-- name: GetSailing :one
SELECT id, route_code, departs_at, capacity, adult_fare_cents FROM sailings WHERE id = $1;

-- name: ListSailings :many
SELECT id, route_code, departs_at, capacity, adult_fare_cents FROM sailings
WHERE route_code = $1 AND departs_at >= $2 AND departs_at < $3
ORDER BY departs_at;

-- name: SeatsTaken :one
SELECT COALESCE(SUM(seats), 0)::bigint FROM bookings WHERE sailing_id = $1 AND status = 'confirmed';
