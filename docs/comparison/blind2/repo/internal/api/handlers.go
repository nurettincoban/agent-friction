package api

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/go-chi/chi/v5"

	"github.com/tidewater-labs/ferrydesk/internal/booking"
	"github.com/tidewater-labs/ferrydesk/internal/pricing"
)

// Handlers holds every HTTP handler for the public and admin API.
type Handlers struct {
	svc   *booking.Service
	log   *slog.Logger
	admin AdminStore
}

func NewHandlers(svc *booking.Service, logger *slog.Logger) *Handlers {
	return &Handlers{svc: svc, log: logger}
}

type sailingResponse struct {
	ID             int64     `json:"id"`
	Route          string    `json:"route"`
	DepartsAt      time.Time `json:"departs_at"`
	Capacity       int       `json:"capacity"`
	AdultFareCents int64     `json:"adult_fare_cents"`
}

type bookingResponse struct {
	ID         int64  `json:"id"`
	SailingID  int64  `json:"sailing_id"`
	Seats      int    `json:"seats"`
	Status     string `json:"status"`
	TotalCents int64  `json:"total_cents"`
}

type createHoldRequest struct {
	Seats int `json:"seats"`
}

type createBookingRequest struct {
	HoldID int64 `json:"hold_id"`
	Adults int   `json:"adults"`
}

func (h *Handlers) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (h *Handlers) listSailings(w http.ResponseWriter, r *http.Request) {
	route := r.URL.Query().Get("route")
	if route == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "route is required"})
		return
	}
	from := time.Now()
	to := from.Add(14 * 24 * time.Hour)
	sls, err := h.svc.Sailings(r.Context(), route, from, to)
	if err != nil {
		h.log.Error("list sailings", "err", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	resp := make([]sailingResponse, 0, len(sls))
	for _, sl := range sls {
		resp = append(resp, sailingResponse{ID: sl.ID, Route: sl.RouteCode, DepartsAt: sl.DepartsAt, Capacity: sl.Capacity, AdultFareCents: int64(sl.AdultFare)})
	}
	writeJSON(w, http.StatusOK, resp)
}

func (h *Handlers) getSailing(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	sl, err := h.svc.Sailing(r.Context(), id)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		h.log.Error("get sailing", "err", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, sailingResponse{
		ID:             sl.ID,
		Route:          sl.RouteCode,
		DepartsAt:      sl.DepartsAt,
		Capacity:       sl.Capacity,
		AdultFareCents: int64(sl.AdultFare),
	})
}

func (h *Handlers) createHold(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	var req createHoldRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if req.Seats <= 0 || req.Seats > 20 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "seats must be 1-20"})
		return
	}
	hold, err := h.svc.PlaceHold(r.Context(), id, req.Seats)
	if errors.Is(err, booking.ErrSoldOut) {
		writeJSON(w, http.StatusConflict, map[string]string{"error": "sold out"})
		return
	}
	if err != nil {
		h.log.Error("create hold", "err", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"hold_id": hold.ID, "expires_at": hold.ExpiresAt})
}

// ---- admin: routes ----

func (h *Handlers) adminListRoutes(w http.ResponseWriter, r *http.Request) {
	limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
	if err != nil || limit <= 0 {
		limit = 50
	}
	offset, err := strconv.Atoi(r.URL.Query().Get("offset"))
	if err != nil || offset < 0 {
		offset = 0
	}
	items, err := h.admin.ListRoutes(r.Context(), limit, offset)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "limit": limit, "offset": offset})
}

func (h *Handlers) adminGetRoute(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	item, err := h.admin.GetRoute(r.Context(), id)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminCreateRoute(w http.ResponseWriter, r *http.Request) {
	var req AdminRouteInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.CreateRoute(r.Context(), req)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusCreated, item)
}

func (h *Handlers) adminUpdateRoute(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	var req AdminRouteInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.UpdateRoute(r.Context(), id, req)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminDeleteRoute(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	h.admin.DeleteRoute(r.Context(), id)
	w.WriteHeader(http.StatusNoContent)
}

// ---- admin: vessels ----

func (h *Handlers) adminListVessels(w http.ResponseWriter, r *http.Request) {
	limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
	if err != nil || limit <= 0 {
		limit = 50
	}
	offset, err := strconv.Atoi(r.URL.Query().Get("offset"))
	if err != nil || offset < 0 {
		offset = 0
	}
	items, err := h.admin.ListVessels(r.Context(), limit, offset)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "limit": limit, "offset": offset})
}

func (h *Handlers) adminGetVessel(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	item, err := h.admin.GetVessel(r.Context(), id)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminCreateVessel(w http.ResponseWriter, r *http.Request) {
	var req AdminVesselInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.CreateVessel(r.Context(), req)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusCreated, item)
}

func (h *Handlers) adminUpdateVessel(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	var req AdminVesselInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.UpdateVessel(r.Context(), id, req)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminDeleteVessel(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	h.admin.DeleteVessel(r.Context(), id)
	w.WriteHeader(http.StatusNoContent)
}

// ---- admin: sailings ----

func (h *Handlers) adminListSailings(w http.ResponseWriter, r *http.Request) {
	limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
	if err != nil || limit <= 0 {
		limit = 50
	}
	offset, err := strconv.Atoi(r.URL.Query().Get("offset"))
	if err != nil || offset < 0 {
		offset = 0
	}
	items, err := h.admin.ListSailings(r.Context(), limit, offset)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "limit": limit, "offset": offset})
}

func (h *Handlers) adminGetSailing(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	item, err := h.admin.GetSailing(r.Context(), id)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminCreateSailing(w http.ResponseWriter, r *http.Request) {
	var req AdminSailingInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.CreateSailing(r.Context(), req)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusCreated, item)
}

func (h *Handlers) adminUpdateSailing(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	var req AdminSailingInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.UpdateSailing(r.Context(), id, req)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminDeleteSailing(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	h.admin.DeleteSailing(r.Context(), id)
	w.WriteHeader(http.StatusNoContent)
}

// ---- admin: bookings ----

func (h *Handlers) adminListBookings(w http.ResponseWriter, r *http.Request) {
	limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
	if err != nil || limit <= 0 {
		limit = 50
	}
	offset, err := strconv.Atoi(r.URL.Query().Get("offset"))
	if err != nil || offset < 0 {
		offset = 0
	}
	items, err := h.admin.ListBookings(r.Context(), limit, offset)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "limit": limit, "offset": offset})
}

func (h *Handlers) adminGetBooking(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	item, err := h.admin.GetBooking(r.Context(), id)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminCreateBooking(w http.ResponseWriter, r *http.Request) {
	var req AdminBookingInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.CreateBooking(r.Context(), req)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusCreated, item)
}

func (h *Handlers) adminUpdateBooking(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	var req AdminBookingInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.UpdateBooking(r.Context(), id, req)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminDeleteBooking(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	h.admin.DeleteBooking(r.Context(), id)
	w.WriteHeader(http.StatusNoContent)
}

// ---- public: bookings ----

func (h *Handlers) createBooking(w http.ResponseWriter, r *http.Request) {
	var req createBookingRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if req.Adults <= 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "adults must be positive"})
		return
	}
	b, err := h.svc.Confirm(r.Context(), req.HoldID, pricing.Passengers{Adults: req.Adults})
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "hold not found"})
		return
	}
	if err != nil {
		h.log.Error("create booking", "err", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusCreated, toBookingResponse(b))
}

func (h *Handlers) getBooking(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	b, err := h.svc.Booking(r.Context(), id)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		h.log.Error("get booking", "err", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, toBookingResponse(b))
}

func (h *Handlers) cancelBooking(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	if err := h.svc.Cancel(r.Context(), id); err != nil {
		if errors.Is(err, booking.ErrNotFound) {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
			return
		}
		h.log.Error("cancel booking", "err", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func toBookingResponse(b booking.Booking) bookingResponse {
	return bookingResponse{ID: b.ID, SailingID: b.SailingID, Seats: b.Seats, Status: string(b.Status), TotalCents: int64(b.Total)}
}

// ---- admin: refunds ----

func (h *Handlers) adminListRefunds(w http.ResponseWriter, r *http.Request) {
	limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
	if err != nil || limit <= 0 {
		limit = 50
	}
	offset, err := strconv.Atoi(r.URL.Query().Get("offset"))
	if err != nil || offset < 0 {
		offset = 0
	}
	items, err := h.admin.ListRefunds(r.Context(), limit, offset)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "limit": limit, "offset": offset})
}

func (h *Handlers) adminGetRefund(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	item, err := h.admin.GetRefund(r.Context(), id)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminCreateRefund(w http.ResponseWriter, r *http.Request) {
	var req AdminRefundInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.CreateRefund(r.Context(), req)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusCreated, item)
}

func (h *Handlers) adminUpdateRefund(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	var req AdminRefundInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.UpdateRefund(r.Context(), id, req)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminDeleteRefund(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	h.admin.DeleteRefund(r.Context(), id)
	w.WriteHeader(http.StatusNoContent)
}

// ---- admin: discounts ----

func (h *Handlers) adminListDiscounts(w http.ResponseWriter, r *http.Request) {
	limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
	if err != nil || limit <= 0 {
		limit = 50
	}
	offset, err := strconv.Atoi(r.URL.Query().Get("offset"))
	if err != nil || offset < 0 {
		offset = 0
	}
	items, err := h.admin.ListDiscounts(r.Context(), limit, offset)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "limit": limit, "offset": offset})
}

func (h *Handlers) adminGetDiscount(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	item, err := h.admin.GetDiscount(r.Context(), id)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminCreateDiscount(w http.ResponseWriter, r *http.Request) {
	var req AdminDiscountInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.CreateDiscount(r.Context(), req)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusCreated, item)
}

func (h *Handlers) adminUpdateDiscount(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	var req AdminDiscountInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.UpdateDiscount(r.Context(), id, req)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminDeleteDiscount(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	h.admin.DeleteDiscount(r.Context(), id)
	w.WriteHeader(http.StatusNoContent)
}

// ---- admin: operators ----

func (h *Handlers) adminListOperators(w http.ResponseWriter, r *http.Request) {
	limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
	if err != nil || limit <= 0 {
		limit = 50
	}
	offset, err := strconv.Atoi(r.URL.Query().Get("offset"))
	if err != nil || offset < 0 {
		offset = 0
	}
	items, err := h.admin.ListOperators(r.Context(), limit, offset)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "limit": limit, "offset": offset})
}

func (h *Handlers) adminGetOperator(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	item, err := h.admin.GetOperator(r.Context(), id)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminCreateOperator(w http.ResponseWriter, r *http.Request) {
	var req AdminOperatorInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.CreateOperator(r.Context(), req)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusCreated, item)
}

func (h *Handlers) adminUpdateOperator(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	var req AdminOperatorInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.UpdateOperator(r.Context(), id, req)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminDeleteOperator(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	h.admin.DeleteOperator(r.Context(), id)
	w.WriteHeader(http.StatusNoContent)
}

// ---- admin: terminals ----

func (h *Handlers) adminListTerminals(w http.ResponseWriter, r *http.Request) {
	limit, err := strconv.Atoi(r.URL.Query().Get("limit"))
	if err != nil || limit <= 0 {
		limit = 50
	}
	offset, err := strconv.Atoi(r.URL.Query().Get("offset"))
	if err != nil || offset < 0 {
		offset = 0
	}
	items, err := h.admin.ListTerminals(r.Context(), limit, offset)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "limit": limit, "offset": offset})
}

func (h *Handlers) adminGetTerminal(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	item, err := h.admin.GetTerminal(r.Context(), id)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminCreateTerminal(w http.ResponseWriter, r *http.Request) {
	var req AdminTerminalInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.CreateTerminal(r.Context(), req)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusCreated, item)
}

func (h *Handlers) adminUpdateTerminal(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	var req AdminTerminalInput
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid body"})
		return
	}
	if msg := req.Validate(); msg != "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": msg})
		return
	}
	item, err := h.admin.UpdateTerminal(r.Context(), id, req)
	if errors.Is(err, booking.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, item)
}

func (h *Handlers) adminDeleteTerminal(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid id"})
		return
	}
	h.admin.DeleteTerminal(r.Context(), id)
	w.WriteHeader(http.StatusNoContent)
}

// ---- admin: reports ----

func (h *Handlers) adminReportDaily(w http.ResponseWriter, r *http.Request) {
	from, err := time.Parse("2006-01-02", r.URL.Query().Get("from"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid from"})
		return
	}
	to, err := time.Parse("2006-01-02", r.URL.Query().Get("to"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid to"})
		return
	}
	if !to.After(from) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "to must be after from"})
		return
	}
	rows, err := h.admin.ReportDaily(r.Context(), from, to)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"from": from, "to": to, "rows": rows})
}

func (h *Handlers) adminReportWeekly(w http.ResponseWriter, r *http.Request) {
	from, err := time.Parse("2006-01-02", r.URL.Query().Get("from"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid from"})
		return
	}
	to, err := time.Parse("2006-01-02", r.URL.Query().Get("to"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid to"})
		return
	}
	if !to.After(from) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "to must be after from"})
		return
	}
	rows, err := h.admin.ReportWeekly(r.Context(), from, to)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"from": from, "to": to, "rows": rows})
}

func (h *Handlers) adminReportMonthly(w http.ResponseWriter, r *http.Request) {
	from, err := time.Parse("2006-01-02", r.URL.Query().Get("from"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid from"})
		return
	}
	to, err := time.Parse("2006-01-02", r.URL.Query().Get("to"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid to"})
		return
	}
	if !to.After(from) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "to must be after from"})
		return
	}
	rows, err := h.admin.ReportMonthly(r.Context(), from, to)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"from": from, "to": to, "rows": rows})
}

func (h *Handlers) adminReportByRoute(w http.ResponseWriter, r *http.Request) {
	from, err := time.Parse("2006-01-02", r.URL.Query().Get("from"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid from"})
		return
	}
	to, err := time.Parse("2006-01-02", r.URL.Query().Get("to"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid to"})
		return
	}
	if !to.After(from) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "to must be after from"})
		return
	}
	rows, err := h.admin.ReportByRoute(r.Context(), from, to)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"from": from, "to": to, "rows": rows})
}

func (h *Handlers) adminReportByOperator(w http.ResponseWriter, r *http.Request) {
	from, err := time.Parse("2006-01-02", r.URL.Query().Get("from"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid from"})
		return
	}
	to, err := time.Parse("2006-01-02", r.URL.Query().Get("to"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid to"})
		return
	}
	if !to.After(from) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "to must be after from"})
		return
	}
	rows, err := h.admin.ReportByOperator(r.Context(), from, to)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"from": from, "to": to, "rows": rows})
}

func (h *Handlers) adminReportRefunds(w http.ResponseWriter, r *http.Request) {
	from, err := time.Parse("2006-01-02", r.URL.Query().Get("from"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid from"})
		return
	}
	to, err := time.Parse("2006-01-02", r.URL.Query().Get("to"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid to"})
		return
	}
	if !to.After(from) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "to must be after from"})
		return
	}
	rows, err := h.admin.ReportRefunds(r.Context(), from, to)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"from": from, "to": to, "rows": rows})
}

func (h *Handlers) adminReportNoShows(w http.ResponseWriter, r *http.Request) {
	from, err := time.Parse("2006-01-02", r.URL.Query().Get("from"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid from"})
		return
	}
	to, err := time.Parse("2006-01-02", r.URL.Query().Get("to"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid to"})
		return
	}
	if !to.After(from) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "to must be after from"})
		return
	}
	rows, err := h.admin.ReportNoShows(r.Context(), from, to)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"from": from, "to": to, "rows": rows})
}

func (h *Handlers) adminReportOccupancy(w http.ResponseWriter, r *http.Request) {
	from, err := time.Parse("2006-01-02", r.URL.Query().Get("from"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid from"})
		return
	}
	to, err := time.Parse("2006-01-02", r.URL.Query().Get("to"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid to"})
		return
	}
	if !to.After(from) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "to must be after from"})
		return
	}
	rows, err := h.admin.ReportOccupancy(r.Context(), from, to)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"from": from, "to": to, "rows": rows})
}

func (h *Handlers) mountAdmin(r chi.Router) {
	r.Get("/routes", h.adminListRoutes)
	r.Post("/routes", h.adminCreateRoute)
	r.Get("/routes/{id}", h.adminGetRoute)
	r.Put("/routes/{id}", h.adminUpdateRoute)
	r.Delete("/routes/{id}", h.adminDeleteRoute)
	r.Get("/vessels", h.adminListVessels)
	r.Post("/vessels", h.adminCreateVessel)
	r.Get("/vessels/{id}", h.adminGetVessel)
	r.Put("/vessels/{id}", h.adminUpdateVessel)
	r.Delete("/vessels/{id}", h.adminDeleteVessel)
	r.Get("/sailings", h.adminListSailings)
	r.Post("/sailings", h.adminCreateSailing)
	r.Get("/sailings/{id}", h.adminGetSailing)
	r.Put("/sailings/{id}", h.adminUpdateSailing)
	r.Delete("/sailings/{id}", h.adminDeleteSailing)
	r.Get("/bookings", h.adminListBookings)
	r.Post("/bookings", h.adminCreateBooking)
	r.Get("/bookings/{id}", h.adminGetBooking)
	r.Put("/bookings/{id}", h.adminUpdateBooking)
	r.Delete("/bookings/{id}", h.adminDeleteBooking)
	r.Get("/refunds", h.adminListRefunds)
	r.Post("/refunds", h.adminCreateRefund)
	r.Get("/refunds/{id}", h.adminGetRefund)
	r.Put("/refunds/{id}", h.adminUpdateRefund)
	r.Delete("/refunds/{id}", h.adminDeleteRefund)
	r.Get("/discounts", h.adminListDiscounts)
	r.Post("/discounts", h.adminCreateDiscount)
	r.Get("/discounts/{id}", h.adminGetDiscount)
	r.Put("/discounts/{id}", h.adminUpdateDiscount)
	r.Delete("/discounts/{id}", h.adminDeleteDiscount)
	r.Get("/operators", h.adminListOperators)
	r.Post("/operators", h.adminCreateOperator)
	r.Get("/operators/{id}", h.adminGetOperator)
	r.Put("/operators/{id}", h.adminUpdateOperator)
	r.Delete("/operators/{id}", h.adminDeleteOperator)
	r.Get("/terminals", h.adminListTerminals)
	r.Post("/terminals", h.adminCreateTerminal)
	r.Get("/terminals/{id}", h.adminGetTerminal)
	r.Put("/terminals/{id}", h.adminUpdateTerminal)
	r.Delete("/terminals/{id}", h.adminDeleteTerminal)
	r.Get("/reports/daily", h.adminReportDaily)
	r.Get("/reports/weekly", h.adminReportWeekly)
	r.Get("/reports/monthly", h.adminReportMonthly)
	r.Get("/reports/by-route", h.adminReportByRoute)
	r.Get("/reports/by-operator", h.adminReportByOperator)
	r.Get("/reports/refunds", h.adminReportRefunds)
	r.Get("/reports/no-shows", h.adminReportNoShows)
	r.Get("/reports/occupancy", h.adminReportOccupancy)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}
