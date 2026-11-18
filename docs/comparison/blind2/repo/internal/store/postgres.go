package store

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/tidewater-labs/ferrydesk/internal/booking"
	"github.com/tidewater-labs/ferrydesk/internal/db"
	"github.com/tidewater-labs/ferrydesk/internal/pricing"
)

type Postgres struct {
	pool *pgxpool.Pool
	q    *db.Queries
}

func Open(ctx context.Context, url string) (*Postgres, error) {
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	return &Postgres{pool: pool, q: db.New(pool)}, nil
}

func (p *Postgres) Close() { p.pool.Close() }

func notFound(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return booking.ErrNotFound
	}
	return err
}

func (p *Postgres) GetSailing(ctx context.Context, id int64) (booking.Sailing, error) {
	row, err := p.q.GetSailing(ctx, id)
	if err != nil {
		return booking.Sailing{}, notFound(err)
	}
	return booking.Sailing{
		ID:        row.ID,
		RouteCode: row.RouteCode,
		DepartsAt: row.DepartsAt.Time,
		Capacity:  int(row.Capacity),
		AdultFare: pricing.Cents(row.AdultFareCents),
	}, nil
}

func (p *Postgres) ListSailings(ctx context.Context, route string, from, to time.Time) ([]booking.Sailing, error) {
	rows, err := p.q.ListSailings(ctx, db.ListSailingsParams{RouteCode: route, DepartsAt: ts(from), DepartsAt_2: ts(to)})
	if err != nil {
		return nil, err
	}
	out := make([]booking.Sailing, 0, len(rows))
	for _, r := range rows {
		out = append(out, booking.Sailing{ID: r.ID, RouteCode: r.RouteCode, DepartsAt: r.DepartsAt.Time, Capacity: int(r.Capacity), AdultFare: pricing.Cents(r.AdultFareCents)})
	}
	return out, nil
}

func (p *Postgres) SeatsTaken(ctx context.Context, sailingID int64) (int, error) {
	n, err := p.q.SeatsTaken(ctx, sailingID)
	return int(n), err
}

func (p *Postgres) ActiveHolds(ctx context.Context, sailingID int64) ([]booking.Hold, error) {
	rows, err := p.q.ActiveHolds(ctx, sailingID)
	if err != nil {
		return nil, err
	}
	out := make([]booking.Hold, 0, len(rows))
	for _, r := range rows {
		out = append(out, booking.Hold{ID: r.ID, SailingID: r.SailingID, Seats: int(r.Seats), CreatedAt: r.CreatedAt.Time, ExpiresAt: r.ExpiresAt.Time})
	}
	return out, nil
}

func (p *Postgres) CreateHold(ctx context.Context, h booking.Hold) (booking.Hold, error) {
	id, err := p.q.CreateHold(ctx, db.CreateHoldParams{SailingID: h.SailingID, Seats: int32(h.Seats), CreatedAt: ts(h.CreatedAt), ExpiresAt: ts(h.ExpiresAt)})
	h.ID = id
	return h, err
}

func (p *Postgres) GetBooking(ctx context.Context, id int64) (booking.Booking, error) {
	r, err := p.q.GetBooking(ctx, id)
	if err != nil {
		return booking.Booking{}, notFound(err)
	}
	return booking.Booking{ID: r.ID, SailingID: r.SailingID, Seats: int(r.Seats), Status: booking.Status(r.Status), Total: pricing.Cents(r.TotalCents), CreatedAt: r.CreatedAt.Time}, nil
}

func (p *Postgres) CreateBooking(ctx context.Context, b booking.Booking) (booking.Booking, error) {
	id, err := p.q.CreateBooking(ctx, db.CreateBookingParams{SailingID: b.SailingID, Seats: int32(b.Seats), Status: string(b.Status), TotalCents: int64(b.Total)})
	b.ID = id
	return b, err
}

func (p *Postgres) SetBookingStatus(ctx context.Context, id int64, s booking.Status) error {
	return p.q.SetBookingStatus(ctx, db.SetBookingStatusParams{ID: id, Status: string(s)})
}

func (p *Postgres) CreateRefund(ctx context.Context, bookingID int64, amount pricing.Cents) error {
	return p.q.CreateRefund(ctx, db.CreateRefundParams{BookingID: bookingID, AmountCents: int64(amount)})
}
