package pricing

import "testing"

func TestFareAdults(t *testing.T) {
	r := Route{Code: "WLG-PCN", AdultFare: 7450}
	if got := Fare(r, Passengers{Adults: 2}); got != 14900 {
		t.Fatalf("got %v", got)
	}
}

func TestApplyBasisPointsRoundsHalfUp(t *testing.T) {
	if got := ApplyBasisPoints(1, 5000); got != 1 {
		t.Fatalf("got %d", got)
	}
}

func TestApplyDiscounts(t *testing.T) {
	got := ApplyDiscounts(10000, []Discount{{Code: "SENIOR", Bps: 2000}})
	if got != 8000 {
		t.Fatalf("got %v", got)
	}
}
