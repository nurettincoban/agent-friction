package pricing

import "fmt"

// Cents is an amount of money in minor units (NZD cents). All prices,
// totals, discounts and refunds are carried as Cents end to end; the API
// serialises them as integers.
type Cents int64

func (c Cents) String() string {
	sign := ""
	if c < 0 {
		sign, c = "-", -c
	}
	return fmt.Sprintf("%s$%d.%02d", sign, c/100, c%100)
}

// ApplyBasisPoints returns c * bps / 10000, rounded half up.
// 1500 bps is 15%.
func ApplyBasisPoints(c Cents, bps int64) Cents {
	n := int64(c) * bps
	q, r := n/10000, n%10000
	if r*2 >= 10000 {
		q++
	}
	return Cents(q)
}
