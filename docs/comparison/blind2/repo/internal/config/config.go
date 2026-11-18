package config

import (
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"time"
)

// envPrefix is prepended to every variable name read by Load.
const envPrefix = "FERRYDESK_"

type Config struct {
	Port        int
	DatabaseURL string
	LogLevel    string
	HoldTTL     time.Duration
}

func get(name, def string) string {
	if v, ok := os.LookupEnv(envPrefix + name); ok {
		return v
	}
	return def
}

// Load builds a Config from the environment.
func Load() (Config, error) {
	port, err := strconv.Atoi(get("PORT", "8080"))
	if err != nil {
		return Config{}, fmt.Errorf("PORT: %w", err)
	}
	ttl, err := time.ParseDuration(get("HOLD_TTL", "15m"))
	if err != nil {
		return Config{}, fmt.Errorf("HOLD_TTL: %w", err)
	}
	return Config{
		Port:        port,
		DatabaseURL: get("DATABASE_URL", "postgres://ferry:ferry@localhost:5432/ferrydesk?sslmode=disable"),
		LogLevel:    get("LOG_LEVEL", "info"),
		HoldTTL:     ttl,
	}, nil
}

func (c Config) SlogLevel() slog.Level {
	switch c.LogLevel {
	case "debug":
		return slog.LevelDebug
	case "warn":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
