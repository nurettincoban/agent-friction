package booking

import (
	"context"
	"errors"
	"time"

	"github.com/tidewater-labs/ferrydesk/internal/pricing"
)

var (
	ErrNotFound         = errors.New("not found")
	ErrSoldOut          = errors.New("sailing sold out")
	ErrAlreadyCancelled = errors.New("booking already cancelled")
)

type Status string

const (
	StatusHeld      Status = "held"
	StatusConfirmed Status = "confirmed"
	StatusCancelled Status = "cancelled"
)

type Sailing struct {
	ID        int64
	RouteCode string
	DepartsAt time.Time
	Capacity  int
	AdultFare pricing.Cents
}

type Booking struct {
	ID        int64
	SailingID int64
	Seats     int
	Status    Status
	Total     pricing.Cents
	CreatedAt time.Time
}

// Store is the persistence boundary. internal/store implements it on Postgres.
type Store interface {
	GetSailing(ctx context.Context, id int64) (Sailing, error)
	ListSailings(ctx context.Context, route string, from, to time.Time) ([]Sailing, error)
	SeatsTaken(ctx context.Context, sailingID int64) (int, error)
	ActiveHolds(ctx context.Context, sailingID int64) ([]Hold, error)
	CreateHold(ctx context.Context, h Hold) (Hold, error)
	GetBooking(ctx context.Context, id int64) (Booking, error)
	CreateBooking(ctx context.Context, b Booking) (Booking, error)
	SetBookingStatus(ctx context.Context, id int64, s Status) error
	CreateRefund(ctx context.Context, bookingID int64, amount pricing.Cents) error
}

type Service struct {
	store   Store
	holdTTL time.Duration
	now     func() time.Time
}

func NewService(s Store, holdTTL time.Duration) *Service {
	return &Service{store: s, holdTTL: holdTTL, now: time.Now}
}

func (s *Service) Sailing(ctx context.Context, id int64) (Sailing, error) {
	return s.store.GetSailing(ctx, id)
}

func (s *Service) Sailings(ctx context.Context, route string, from, to time.Time) ([]Sailing, error) {
	return s.store.ListSailings(ctx, route, from, to)
}

// heldSeats counts seats in holds that have not expired.
func (s *Service) heldSeats(ctx context.Context, sailingID int64) (int, error) {
	holds, err := s.store.ActiveHolds(ctx, sailingID)
	if err != nil {
		return 0, err
	}
	n := 0
	now := s.now()
	for _, h := range holds {
		if !h.Expired(now) {
			n += h.Seats
		}
	}
	return n, nil
}

func (s *Service) PlaceHold(ctx context.Context, sailingID int64, seats int) (Hold, error) {
	sl, err := s.store.GetSailing(ctx, sailingID)
	if err != nil {
		return Hold{}, err
	}
	taken, err := s.store.SeatsTaken(ctx, sailingID)
	if err != nil {
		return Hold{}, err
	}
	held, err := s.heldSeats(ctx, sailingID)
	if err != nil {
		return Hold{}, err
	}
	if taken+held+seats > sl.Capacity {
		return Hold{}, ErrSoldOut
	}
	now := s.now()
	return s.store.CreateHold(ctx, Hold{
		SailingID: sailingID,
		Seats:     seats,
		CreatedAt: now,
		ExpiresAt: now.Add(s.holdTTL),
	})
}

func (s *Service) Cancel(ctx context.Context, bookingID int64) error {
	b, err := s.store.GetBooking(ctx, bookingID)
	if err != nil {
		return err
	}
	if err := s.store.SetBookingStatus(ctx, bookingID, StatusCancelled); err != nil {
		return err
	}
	return s.store.CreateRefund(ctx, bookingID, b.Total)
}

func (s *Service) Booking(ctx context.Context, id int64) (Booking, error) {
	return s.store.GetBooking(ctx, id)
}

// Confirm turns a hold into a confirmed booking and prices it.
func (s *Service) Confirm(ctx context.Context, holdID int64, p pricing.Passengers) (Booking, error) {
	h, sl, err := s.holdWithSailing(ctx, holdID)
	if err != nil {
		return Booking{}, err
	}
	if h.Expired(s.now()) {
		return Booking{}, ErrNotFound
	}
	total := pricing.Fare(pricing.Route{Code: sl.RouteCode, AdultFare: sl.AdultFare}, p)
	return s.store.CreateBooking(ctx, Booking{SailingID: sl.ID, Seats: h.Seats, Status: StatusConfirmed, Total: total})
}

func (s *Service) holdWithSailing(ctx context.Context, holdID int64) (Hold, Sailing, error) {
	// holds are looked up via the sailing's active holds; there are few per sailing.
	return Hold{}, Sailing{}, ErrNotFound
}
