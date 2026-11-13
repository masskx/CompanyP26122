package config

import (
	"log/slog"
	"strings"
	"testing"
	"time"
)

func TestLoadDefaults(t *testing.T) {
	clearEnvironment(t)

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}

	if cfg.Addr != defaultAddr {
		t.Errorf("Addr = %q, want %q", cfg.Addr, defaultAddr)
	}
	if cfg.Environment != defaultEnvironment {
		t.Errorf("Environment = %q, want %q", cfg.Environment, defaultEnvironment)
	}
	if cfg.LogLevel != slog.LevelInfo {
		t.Errorf("LogLevel = %v, want %v", cfg.LogLevel, slog.LevelInfo)
	}
	if cfg.ShutdownTimeout != 10*time.Second {
		t.Errorf("ShutdownTimeout = %v, want %v", cfg.ShutdownTimeout, 10*time.Second)
	}
	if cfg.ComputeServiceBaseURL != defaultComputeServiceBaseURL {
		t.Errorf("ComputeServiceBaseURL = %q, want %q",
			cfg.ComputeServiceBaseURL, defaultComputeServiceBaseURL)
	}
	if cfg.ComputeServiceTimeout != 3*time.Minute {
		t.Errorf("ComputeServiceTimeout = %v, want 3m", cfg.ComputeServiceTimeout)
	}
	if cfg.ComputeServiceAPIKey != "" {
		t.Errorf("ComputeServiceAPIKey = %q, want empty", cfg.ComputeServiceAPIKey)
	}
}

func TestLoadOverrides(t *testing.T) {
	t.Setenv("MAIN_SERVER_ADDR", "0.0.0.0:9090")
	t.Setenv("MAIN_SERVER_ENV", "PRODUCTION")
	t.Setenv("MAIN_SERVER_LOG_LEVEL", "debug")
	t.Setenv("MAIN_SERVER_SHUTDOWN_TIMEOUT", "3s")
	t.Setenv("COMPUTE_SERVICE_BASE_URL", "https://compute.example.test:9443/")
	t.Setenv("COMPUTE_SERVICE_TIMEOUT", "45s")
	t.Setenv("COMPUTE_SERVICE_API_KEY", " test-key ")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}

	if cfg.Addr != "0.0.0.0:9090" {
		t.Errorf("Addr = %q, want %q", cfg.Addr, "0.0.0.0:9090")
	}
	if cfg.Environment != "production" {
		t.Errorf("Environment = %q, want %q", cfg.Environment, "production")
	}
	if cfg.LogLevel != slog.LevelDebug {
		t.Errorf("LogLevel = %v, want %v", cfg.LogLevel, slog.LevelDebug)
	}
	if cfg.ShutdownTimeout != 3*time.Second {
		t.Errorf("ShutdownTimeout = %v, want %v", cfg.ShutdownTimeout, 3*time.Second)
	}
	if cfg.ComputeServiceBaseURL != "https://compute.example.test:9443" {
		t.Errorf("ComputeServiceBaseURL = %q, want normalized URL", cfg.ComputeServiceBaseURL)
	}
	if cfg.ComputeServiceTimeout != 45*time.Second {
		t.Errorf("ComputeServiceTimeout = %v, want 45s", cfg.ComputeServiceTimeout)
	}
	if cfg.ComputeServiceAPIKey != "test-key" {
		t.Errorf("ComputeServiceAPIKey = %q, want test-key", cfg.ComputeServiceAPIKey)
	}
}

func TestLoadRejectsInvalidLogLevel(t *testing.T) {
	clearEnvironment(t)
	t.Setenv("MAIN_SERVER_LOG_LEVEL", "verbose")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() error = nil, want an error")
	}
	if !strings.Contains(err.Error(), "MAIN_SERVER_LOG_LEVEL") {
		t.Errorf("Load() error = %q, want it to mention MAIN_SERVER_LOG_LEVEL", err)
	}
}

func TestLoadRejectsInvalidShutdownTimeout(t *testing.T) {
	clearEnvironment(t)
	t.Setenv("MAIN_SERVER_SHUTDOWN_TIMEOUT", "soon")

	_, err := Load()
	if err == nil {
		t.Fatal("Load() error = nil, want an error")
	}
	if !strings.Contains(err.Error(), "MAIN_SERVER_SHUTDOWN_TIMEOUT") {
		t.Errorf("Load() error = %q, want it to mention MAIN_SERVER_SHUTDOWN_TIMEOUT", err)
	}
}

func TestLoadRejectsInvalidComputeServiceConfiguration(t *testing.T) {
	tests := []struct {
		name  string
		key   string
		value string
	}{
		{name: "invalid URL", key: "COMPUTE_SERVICE_BASE_URL", value: "not a URL"},
		{name: "unsupported scheme", key: "COMPUTE_SERVICE_BASE_URL", value: "ftp://compute"},
		{name: "missing host", key: "COMPUTE_SERVICE_BASE_URL", value: "http:///chat"},
		{name: "user information", key: "COMPUTE_SERVICE_BASE_URL", value: "http://user@compute"},
		{name: "invalid timeout", key: "COMPUTE_SERVICE_TIMEOUT", value: "later"},
		{name: "zero timeout", key: "COMPUTE_SERVICE_TIMEOUT", value: "0s"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			clearEnvironment(t)
			t.Setenv(test.key, test.value)

			_, err := Load()
			if err == nil {
				t.Fatalf("Load() error = nil, want an error for %s=%q", test.key, test.value)
			}
			if !strings.Contains(err.Error(), test.key) {
				t.Errorf("Load() error = %q, want it to mention %s", err, test.key)
			}
		})
	}
}

func clearEnvironment(t *testing.T) {
	t.Helper()
	t.Setenv("MAIN_SERVER_ADDR", "")
	t.Setenv("MAIN_SERVER_ENV", "")
	t.Setenv("MAIN_SERVER_LOG_LEVEL", "")
	t.Setenv("MAIN_SERVER_SHUTDOWN_TIMEOUT", "")
	t.Setenv("COMPUTE_SERVICE_BASE_URL", "")
	t.Setenv("COMPUTE_SERVICE_TIMEOUT", "")
	t.Setenv("COMPUTE_SERVICE_API_KEY", "")
}
