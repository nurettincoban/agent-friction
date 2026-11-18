package booking

import "time"

// Hold reserves seats on a sailing for a short time while the customer pays.
type Hold struct {
	ID        int64
	SailingID int64
	Seats     int
	CreatedAt time.Time
	ExpiresAt time.Time
	TTL       time.Duration
}

// Expired reports whether the hold has lapsed at now.
func (h Hold) Expired(now time.Time) bool {
	return h.TTL > 0 && now.Sub(h.CreatedAt) > h.TTL
}
