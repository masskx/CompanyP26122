package config

import (
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"strings"
	"time"
)

const (
	defaultAddr                  = "127.0.0.1:8080"
	defaultEnvironment           = "development"
	defaultLogLevel              = "info"
	defaultShutdownTimeout       = "10s"
	defaultComputeServiceBaseURL = "http://172.16.103.196:8000"
	defaultComputeServiceTimeout = "3m"
)

// Config contains the runtime settings loaded from environment variables.
type Config struct {
	Addr                  string
	Environment           string
	LogLevel              slog.Level
	ShutdownTimeout       time.Duration
	ComputeServiceBaseURL string
	ComputeServiceTimeout time.Duration
	ComputeServiceAPIKey  string
}

// Load reads and validates all supported environment variables.
func Load() (Config, error) {
	cfg := Config{
		Addr:            envOrDefault("MAIN_SERVER_ADDR", defaultAddr),
		Environment:     strings.ToLower(envOrDefault("MAIN_SERVER_ENV", defaultEnvironment)),
		ShutdownTimeout: 0,
	}

	levelText := envOrDefault("MAIN_SERVER_LOG_LEVEL", defaultLogLevel)
	if err := cfg.LogLevel.UnmarshalText([]byte(levelText)); err != nil {
		return Config{}, fmt.Errorf(
			"MAIN_SERVER_LOG_LEVEL %q must be one of debug, info, warn, error: %w",
			levelText,
			err,
		)
	}

	timeoutText := envOrDefault("MAIN_SERVER_SHUTDOWN_TIMEOUT", defaultShutdownTimeout)
	timeout, err := time.ParseDuration(timeoutText)
	if err != nil {
		return Config{}, fmt.Errorf(
			"MAIN_SERVER_SHUTDOWN_TIMEOUT %q must be a valid duration: %w",
			timeoutText,
			err,
		)
	}
	if timeout <= 0 {
		return Config{}, fmt.Errorf(
			"MAIN_SERVER_SHUTDOWN_TIMEOUT %q must be greater than zero",
			timeoutText,
		)
	}
	cfg.ShutdownTimeout = timeout

	baseURL, err := validateBaseURL(envOrDefault(
		"COMPUTE_SERVICE_BASE_URL",
		defaultComputeServiceBaseURL,
	))
	if err != nil {
		return Config{}, err
	}
	cfg.ComputeServiceBaseURL = baseURL

	timeoutText = envOrDefault("COMPUTE_SERVICE_TIMEOUT", defaultComputeServiceTimeout)
	computeTimeout, err := time.ParseDuration(timeoutText)
	if err != nil {
		return Config{}, fmt.Errorf(
			"COMPUTE_SERVICE_TIMEOUT %q must be a valid duration: %w",
			timeoutText,
			err,
		)
	}
	if computeTimeout <= 0 {
		return Config{}, fmt.Errorf(
			"COMPUTE_SERVICE_TIMEOUT %q must be greater than zero",
			timeoutText,
		)
	}
	cfg.ComputeServiceTimeout = computeTimeout
	cfg.ComputeServiceAPIKey = strings.TrimSpace(os.Getenv("COMPUTE_SERVICE_API_KEY"))

	return cfg, nil
}

func validateBaseURL(value string) (string, error) {
	parsed, err := url.Parse(value)
	if err != nil {
		return "", fmt.Errorf("COMPUTE_SERVICE_BASE_URL %q is not a valid URL: %w", value, err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return "", fmt.Errorf("COMPUTE_SERVICE_BASE_URL %q must use http or https", value)
	}
	if parsed.Host == "" {
		return "", fmt.Errorf("COMPUTE_SERVICE_BASE_URL %q must include a host", value)
	}
	if parsed.User != nil {
		return "", fmt.Errorf(
			"COMPUTE_SERVICE_BASE_URL %q must not include user information",
			value,
		)
	}
	if parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", fmt.Errorf(
			"COMPUTE_SERVICE_BASE_URL %q must not include a query or fragment",
			value,
		)
	}
	return strings.TrimRight(parsed.String(), "/"), nil
}

func envOrDefault(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}
