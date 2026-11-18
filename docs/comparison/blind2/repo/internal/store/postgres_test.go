package store

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/tidewater-labs/ferrydesk/internal/booking"
)

func testStore(t *testing.T) *Postgres {
	t.Helper()
	url := os.Getenv("FERRYDESK_DATABASE_URL")
	if url == "" {
		url = "postgres://ferry:ferry@localhost:5432/ferrydesk?sslmode=disable"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	p, err := Open(ctx, url)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(p.Close)
	return p
}

func TestCreateAndGetBooking(t *testing.T) {
	p := testStore(t)
	ctx := context.Background()
	b, err := p.CreateBooking(ctx, booking.Booking{SailingID: 1, Seats: 2, Status: booking.StatusConfirmed, Total: 14900})
	if err != nil {
		t.Fatal(err)
	}
	got, err := p.GetBooking(ctx, b.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got.Total != 14900 {
		t.Fatalf("total = %v", got.Total)
	}
}

func TestActiveHolds(t *testing.T) {
	p := testStore(t)
	if _, err := p.ActiveHolds(context.Background(), 1); err != nil {
		t.Fatal(err)
	}
}
