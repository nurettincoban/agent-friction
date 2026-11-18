package api

import (
	"context"
	"time"
)

// AdminStore backs the /admin endpoints. Items are returned as plain maps
// because the admin UI renders them generically.
type AdminStore interface {
	ListRoutes(ctx context.Context, limit, offset int) ([]map[string]any, error)
	GetRoute(ctx context.Context, id int64) (map[string]any, error)
	CreateRoute(ctx context.Context, in AdminRouteInput) (map[string]any, error)
	UpdateRoute(ctx context.Context, id int64, in AdminRouteInput) (map[string]any, error)
	DeleteRoute(ctx context.Context, id int64) error
	// ... same five methods for Vessel, Sailing, Booking, Refund, Discount, Operator, Terminal

	ReportDaily(ctx context.Context, from, to time.Time) ([]map[string]any, error)
	// ... ReportWeekly, ReportMonthly, ReportByRoute, ReportByOperator, ReportRefunds, ReportNoShows, ReportOccupancy
}

type AdminRouteInput struct {
	Code           string `json:"code"`
	Name           string `json:"name"`
	AdultFareCents int64  `json:"adult_fare_cents"`
}

func (in AdminRouteInput) Validate() string {
	if in.Code == "" || in.Name == "" {
		return "code and name are required"
	}
	if in.AdultFareCents <= 0 {
		return "adult_fare_cents must be positive"
	}
	return ""
}
