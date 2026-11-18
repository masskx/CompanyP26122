package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/url"
	"os"
	pathpkg "path"
	"strings"
	"time"
)

const (
	defaultConfigPath            = "config.json"
	defaultAddr                  = "127.0.0.1:8080"
	defaultEnvironment           = "development"
	defaultLogLevel              = "info"
	defaultShutdownTimeout       = "10s"
	defaultComputeChatPath       = "/api/v1/chat"
	defaultComputeServiceTimeout = "3m"
)

type config struct {
	Addr                string
	Environment         string
	LogLevel            slog.Level
	ShutdownTimeout     time.Duration
	ComputeURL          *url.URL
	ComputeAssistantURL *url.URL
	ComputeTimeout      time.Duration
	ComputeAPIKey       string
}

type fileConfig struct {
	ListenAddr      string            `json:"listen_addr"`
	Environment     string            `json:"environment"`
	LogLevel        string            `json:"log_level"`
	ShutdownTimeout string            `json:"shutdown_timeout"`
	Compute         computeFileConfig `json:"compute"`
}

type computeFileConfig struct {
	BaseURL       string `json:"base_url"`
	ChatPath      string `json:"chat_path"`
	AssistantPath string `json:"assistant_path"`
	Timeout       string `json:"timeout"`
	APIKey        string `json:"api_key"`
}

func loadConfig(path string) (config, error) {
	file, err := os.Open(path)
	if err != nil {
		return config{}, fmt.Errorf("open config %q: %w", path, err)
	}
	defer file.Close()

	raw := fileConfig{
		ListenAddr:      defaultAddr,
		Environment:     defaultEnvironment,
		LogLevel:        defaultLogLevel,
		ShutdownTimeout: defaultShutdownTimeout,
		Compute: computeFileConfig{
			ChatPath: defaultComputeChatPath,
			Timeout:  defaultComputeServiceTimeout,
		},
	}

	decoder := json.NewDecoder(file)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&raw); err != nil {
		return config{}, fmt.Errorf("decode config %q: %w", path, err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return config{}, fmt.Errorf("config %q must contain one JSON object", path)
	}

	listenAddr := strings.TrimSpace(raw.ListenAddr)
	if listenAddr == "" {
		listenAddr = defaultAddr
	}

	environment := strings.ToLower(strings.TrimSpace(raw.Environment))
	if environment == "" {
		environment = defaultEnvironment
	}

	levelText := strings.TrimSpace(raw.LogLevel)
	if levelText == "" {
		levelText = defaultLogLevel
	}
	var level slog.Level
	if err := level.UnmarshalText([]byte(levelText)); err != nil {
		return config{}, fmt.Errorf(
			"log_level %q must be one of debug, info, warn, error: %w",
			levelText,
			err,
		)
	}

	shutdownTimeout, err := parseConfigDuration(
		"shutdown_timeout",
		raw.ShutdownTimeout,
		defaultShutdownTimeout,
	)
	if err != nil {
		return config{}, err
	}

	baseURL, err := parseComputeBaseURL(raw.Compute.BaseURL)
	if err != nil {
		return config{}, err
	}
	chatPath, err := parseComputeChatPath(raw.Compute.ChatPath)
	if err != nil {
		return config{}, err
	}
	assistantPath := strings.TrimSpace(raw.Compute.AssistantPath)
	if assistantPath == "" {
		assistantPath = pathpkg.Dir(chatPath)
	}
	assistantPath, err = parseComputePath("compute.assistant_path", assistantPath)
	if err != nil {
		return config{}, err
	}
	computeTimeout, err := parseConfigDuration(
		"compute.timeout",
		raw.Compute.Timeout,
		defaultComputeServiceTimeout,
	)
	if err != nil {
		return config{}, err
	}

	return config{
		Addr:                listenAddr,
		Environment:         environment,
		LogLevel:            level,
		ShutdownTimeout:     shutdownTimeout,
		ComputeURL:          baseURL.JoinPath(strings.TrimPrefix(chatPath, "/")),
		ComputeAssistantURL: baseURL.JoinPath(strings.TrimPrefix(assistantPath, "/")),
		ComputeTimeout:      computeTimeout,
		ComputeAPIKey:       strings.TrimSpace(raw.Compute.APIKey),
	}, nil
}

func parseConfigDuration(field, value, fallback string) (time.Duration, error) {
	text := strings.TrimSpace(value)
	if text == "" {
		text = fallback
	}
	duration, err := time.ParseDuration(text)
	if err != nil {
		return 0, fmt.Errorf("%s %q must be a valid duration: %w", field, text, err)
	}
	if duration <= 0 {
		return 0, fmt.Errorf("%s %q must be greater than zero", field, text)
	}
	return duration, nil
}

func parseComputeBaseURL(value string) (*url.URL, error) {
	text := strings.TrimSpace(value)
	if text == "" {
		return nil, fmt.Errorf("compute.base_url must be provided")
	}

	parsed, err := url.Parse(text)
	if err != nil {
		return nil, fmt.Errorf("compute.base_url %q is not a valid URL: %w", text, err)
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return nil, fmt.Errorf("compute.base_url %q must use http or https", text)
	}
	if parsed.Host == "" {
		return nil, fmt.Errorf("compute.base_url %q must include a host", text)
	}
	if parsed.User != nil {
		return nil, fmt.Errorf("compute.base_url %q must not include user information", text)
	}
	if parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, fmt.Errorf("compute.base_url %q must not include a query or fragment", text)
	}
	return parsed, nil
}

func parseComputeChatPath(value string) (string, error) {
	return parseComputePath("compute.chat_path", value)
}

func parseComputePath(field, value string) (string, error) {
	text := strings.TrimSpace(value)
	if text == "" && field == "compute.chat_path" {
		text = defaultComputeChatPath
	}
	if text == "" {
		return "", fmt.Errorf("%s must be provided", field)
	}
	if !strings.HasPrefix(text, "/") {
		return "", fmt.Errorf("%s %q must start with /", field, text)
	}
	if strings.ContainsAny(text, "?#") {
		return "", fmt.Errorf("%s %q must not include a query or fragment", field, text)
	}

	parsed, err := url.ParseRequestURI(text)
	if err != nil {
		return "", fmt.Errorf("%s %q is not a valid request path: %w", field, text, err)
	}
	if parsed.IsAbs() || parsed.Host != "" || parsed.RawQuery != "" || parsed.Fragment != "" {
		return "", fmt.Errorf("%s %q must be a path without host, query, or fragment", field, text)
	}
	return text, nil
}
