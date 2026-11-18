package api

import (
	"log/slog"
	"net/http"

	"github.com/go-chi/chi/v5"
)

func NewRouter(h *Handlers, logger *slog.Logger) http.Handler {
	r := chi.NewRouter()
	r.Use(requestID, accessLog(logger), recoverer(logger))

	r.Get("/healthz", h.health)
	r.Route("/sailings", func(r chi.Router) {
		r.Get("/", h.listSailings)
		r.Get("/{id}", h.getSailing)
		r.Post("/{id}/holds", h.createHold)
	})
	r.Route("/bookings", func(r chi.Router) {
		r.Post("/", h.createBooking)
		r.Get("/{id}", h.getBooking)
		r.Delete("/{id}", h.cancelBooking)
	})
	r.Route("/admin", func(r chi.Router) {
		h.mountAdmin(r)
	})
	return r
}
