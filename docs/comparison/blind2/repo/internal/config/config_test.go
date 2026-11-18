package config

import "testing"

func TestLoadDefaults(t *testing.T) {
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Port != 8080 {
		t.Fatalf("port = %d, want 8080", cfg.Port)
	}
}

func TestLoadPort(t *testing.T) {
	t.Setenv("FERRYDESK_PORT", "9191")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Port != 9191 {
		t.Fatalf("port = %d, want 9191", cfg.Port)
	}
}
