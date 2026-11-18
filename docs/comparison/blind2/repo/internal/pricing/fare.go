package pricing

// Route identifies a crossing and carries its base adult fare.
type Route struct {
	Code      string
	AdultFare Cents
}

// Passengers counts travellers on one booking.
type Passengers struct {
	Adults int
}

// Fare returns the total fare for a booking on the given route.
func Fare(r Route, p Passengers) Cents {
	return r.AdultFare * Cents(p.Adults)
}

// Discount is applied to a fare total, in basis points.
type Discount struct {
	Code string
	Bps  int64
}

// ApplyDiscounts subtracts each discount in order.
func ApplyDiscounts(total Cents, ds []Discount) Cents {
	for _, d := range ds {
		total -= ApplyBasisPoints(total, d.Bps)
	}
	return total
}
