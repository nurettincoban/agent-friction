package booking

import (
	"context"
	"testing"
	"time"

	"github.com/tidewater-labs/ferrydesk/internal/pricing"
)

type fakeStore struct {
	sailing  Sailing
	taken    int
	holds    []Hold
	bookings map[int64]Booking
	refunds  []pricing.Cents
}

func (f *fakeStore) GetSailing(context.Context, int64) (Sailing, error) { return f.sailing, nil }
func (f *fakeStore) ListSailings(context.Context, string, time.Time, time.Time) ([]Sailing, error) {
	return []Sailing{f.sailing}, nil
}
func (f *fakeStore) SeatsTaken(context.Context, int64) (int, error)     { return f.taken, nil }
func (f *fakeStore) ActiveHolds(context.Context, int64) ([]Hold, error) { return f.holds, nil }
func (f *fakeStore) CreateHold(_ context.Context, h Hold) (Hold, error) {
	f.holds = append(f.holds, h)
	return h, nil
}
func (f *fakeStore) GetBooking(_ context.Context, id int64) (Booking, error) {
	b, ok := f.bookings[id]
	if !ok {
		return Booking{}, ErrNotFound
	}
	return b, nil
}
func (f *fakeStore) CreateBooking(_ context.Context, b Booking) (Booking, error) { return b, nil }
func (f *fakeStore) SetBookingStatus(_ context.Context, id int64, s Status) error {
	b := f.bookings[id]
	b.Status = s
	f.bookings[id] = b
	return nil
}
func (f *fakeStore) CreateRefund(_ context.Context, _ int64, amt pricing.Cents) error {
	f.refunds = append(f.refunds, amt)
	return nil
}

func TestPlaceHoldSoldOut(t *testing.T) {
	fs := &fakeStore{sailing: Sailing{ID: 1, Capacity: 10}, taken: 9}
	svc := NewService(fs, 15*time.Minute)
	if _, err := svc.PlaceHold(context.Background(), 1, 2); err != ErrSoldOut {
		t.Fatalf("err = %v, want ErrSoldOut", err)
	}
}

func TestCancelRefundsTotal(t *testing.T) {
	fs := &fakeStore{bookings: map[int64]Booking{7: {ID: 7, Status: StatusConfirmed, Total: 14900}}}
	svc := NewService(fs, 15*time.Minute)
	if err := svc.Cancel(context.Background(), 7); err != nil {
		t.Fatal(err)
	}
	if len(fs.refunds) != 1 || fs.refunds[0] != 14900 {
		t.Fatalf("refunds = %v", fs.refunds)
	}
}
