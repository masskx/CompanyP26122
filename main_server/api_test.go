package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestLoadConfigDefaults(t *testing.T) {
	cfg := loadConfigFromJSON(t, `{"compute":{"base_url":"https://compute.example.test:9443/"}}`)

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
		t.Errorf("ShutdownTimeout = %v, want 10s", cfg.ShutdownTimeout)
	}
	if got := cfg.ComputeURL.String(); got != "https://compute.example.test:9443/api/v1/chat" {
		t.Errorf("ComputeURL = %q, want default chat path", got)
	}
	if got := cfg.ComputeAssistantURL.String(); got != "https://compute.example.test:9443/api/v1" {
		t.Errorf("ComputeAssistantURL = %q, want derived assistant path", got)
	}
	if cfg.ComputeTimeout != 3*time.Minute {
		t.Errorf("ComputeTimeout = %v, want 3m", cfg.ComputeTimeout)
	}
	if cfg.ComputeAPIKey != "" {
		t.Errorf("ComputeAPIKey = %q, want empty", cfg.ComputeAPIKey)
	}
}

func TestLoadConfigOverrides(t *testing.T) {
	cfg := loadConfigFromJSON(t, `{
		"listen_addr":"0.0.0.0:9090",
		"environment":"PRODUCTION",
		"log_level":"debug",
		"shutdown_timeout":"3s",
		"compute":{
			"base_url":"https://compute.example.test:9443/base/",
			"chat_path":"/internal/v1/assistant/chat",
			"timeout":"45s",
			"api_key":" test-key "
		}
	}`)

	if cfg.Addr != "0.0.0.0:9090" {
		t.Errorf("Addr = %q, want 0.0.0.0:9090", cfg.Addr)
	}
	if cfg.Environment != "production" {
		t.Errorf("Environment = %q, want production", cfg.Environment)
	}
	if cfg.LogLevel != slog.LevelDebug {
		t.Errorf("LogLevel = %v, want debug", cfg.LogLevel)
	}
	if cfg.ShutdownTimeout != 3*time.Second {
		t.Errorf("ShutdownTimeout = %v, want 3s", cfg.ShutdownTimeout)
	}
	if got := cfg.ComputeURL.String(); got != "https://compute.example.test:9443/base/internal/v1/assistant/chat" {
		t.Errorf("ComputeURL = %q, want configured chat path", got)
	}
	if got := cfg.ComputeAssistantURL.String(); got != "https://compute.example.test:9443/base/internal/v1/assistant" {
		t.Errorf("ComputeAssistantURL = %q, want derived assistant path", got)
	}
	if cfg.ComputeTimeout != 45*time.Second {
		t.Errorf("ComputeTimeout = %v, want 45s", cfg.ComputeTimeout)
	}
	if cfg.ComputeAPIKey != "test-key" {
		t.Errorf("ComputeAPIKey = %q, want test-key", cfg.ComputeAPIKey)
	}
}

func TestLoadConfigDoesNotReadEnvironment(t *testing.T) {
	t.Setenv("MAIN_SERVER_ADDR", "0.0.0.0:9999")
	t.Setenv("COMPUTE_SERVICE_BASE_URL", "http://environment.invalid")

	cfg := loadConfigFromJSON(t, `{"compute":{"base_url":"http://127.0.0.1:8000"}}`)

	if cfg.Addr != defaultAddr {
		t.Errorf("Addr = %q, want JSON/default value %q", cfg.Addr, defaultAddr)
	}
	if got := cfg.ComputeURL.Host; got != "127.0.0.1:8000" {
		t.Errorf("ComputeURL.Host = %q, want JSON value", got)
	}
}

func TestLoadConfigRejectsInvalidFiles(t *testing.T) {
	tests := []struct {
		name string
		body string
		want string
	}{
		{name: "invalid JSON", body: `{"compute":`, want: "decode config"},
		{name: "unknown top-level field", body: `{"compute":{"base_url":"http://127.0.0.1:8000"},"extra":true}`, want: "unknown field"},
		{name: "unknown compute field", body: `{"compute":{"base_url":"http://127.0.0.1:8000","extra":true}}`, want: "unknown field"},
		{name: "multiple objects", body: `{"compute":{"base_url":"http://127.0.0.1:8000"}}{}`, want: "one JSON object"},
		{name: "missing base URL", body: `{}`, want: "compute.base_url"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := loadConfigFromText(t, test.body)
			if err == nil {
				t.Fatal("loadConfig() error = nil, want an error")
			}
			if !strings.Contains(err.Error(), test.want) {
				t.Errorf("error = %q, want it to contain %q", err, test.want)
			}
		})
	}
}

func TestLoadConfigRejectsInvalidComputeURL(t *testing.T) {
	tests := []struct {
		name  string
		value string
	}{
		{name: "invalid URL", value: "not a URL"},
		{name: "unsupported scheme", value: "ftp://compute"},
		{name: "missing host", value: "http:///chat"},
		{name: "user information", value: "http://user@compute"},
		{name: "query", value: "http://compute:8000/?x=1"},
		{name: "fragment", value: "http://compute:8000/#x"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			body := fmt.Sprintf(`{"compute":{"base_url":%q}}`, test.value)
			_, err := loadConfigFromText(t, body)
			if err == nil {
				t.Fatalf("loadConfig() error = nil for %q", test.value)
			}
			if !strings.Contains(err.Error(), "compute.base_url") {
				t.Errorf("error = %q, want it to mention compute.base_url", err)
			}
		})
	}
}

func TestLoadConfigRejectsInvalidChatPath(t *testing.T) {
	for _, value := range []string{"api/v1/chat", "http://compute/chat", "/chat?x=1", "/chat#x"} {
		t.Run(value, func(t *testing.T) {
			body := fmt.Sprintf(
				`{"compute":{"base_url":"http://127.0.0.1:8000","chat_path":%q}}`,
				value,
			)
			_, err := loadConfigFromText(t, body)
			if err == nil {
				t.Fatalf("loadConfig() error = nil for chat path %q", value)
			}
			if !strings.Contains(err.Error(), "compute.chat_path") {
				t.Errorf("error = %q, want it to mention compute.chat_path", err)
			}
		})
	}
}

func TestLoadConfigUsesConfiguredAssistantPath(t *testing.T) {
	cfg := loadConfigFromJSON(t, `{
		"compute":{
			"base_url":"http://127.0.0.1:8000",
			"chat_path":"/custom/chat",
			"assistant_path":"/custom/assistant"
		}
	}`)

	if got := cfg.ComputeAssistantURL.String(); got !=
		"http://127.0.0.1:8000/custom/assistant" {
		t.Errorf("ComputeAssistantURL = %q, want configured assistant path", got)
	}
}

func TestLoadConfigRejectsInvalidAssistantPath(t *testing.T) {
	for _, value := range []string{"api/v1", "http://compute/api", "/api?x=1", "/api#x"} {
		t.Run(value, func(t *testing.T) {
			body := fmt.Sprintf(
				`{"compute":{"base_url":"http://127.0.0.1:8000","assistant_path":%q}}`,
				value,
			)
			_, err := loadConfigFromText(t, body)
			if err == nil {
				t.Fatalf("loadConfig() error = nil for assistant path %q", value)
			}
			if !strings.Contains(err.Error(), "compute.assistant_path") {
				t.Errorf("error = %q, want it to mention compute.assistant_path", err)
			}
		})
	}
}

func TestLoadConfigRejectsInvalidTimeoutsAndLogLevel(t *testing.T) {
	tests := []struct {
		name string
		body string
		want string
	}{
		{
			name: "shutdown timeout",
			body: `{"shutdown_timeout":"soon","compute":{"base_url":"http://127.0.0.1:8000"}}`,
			want: "shutdown_timeout",
		},
		{
			name: "compute timeout",
			body: `{"compute":{"base_url":"http://127.0.0.1:8000","timeout":"0s"}}`,
			want: "compute.timeout",
		},
		{
			name: "log level",
			body: `{"log_level":"verbose","compute":{"base_url":"http://127.0.0.1:8000"}}`,
			want: "log_level",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := loadConfigFromText(t, test.body)
			if err == nil {
				t.Fatal("loadConfig() error = nil, want an error")
			}
			if !strings.Contains(err.Error(), test.want) {
				t.Errorf("error = %q, want it to contain %q", err, test.want)
			}
		})
	}
}

func TestLoadConfigMissingFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "missing.json")
	_, err := loadConfig(path)
	if err == nil {
		t.Fatal("loadConfig() error = nil, want an error")
	}
	if !strings.Contains(err.Error(), path) {
		t.Errorf("error = %q, want it to mention %s", err, path)
	}
}

func TestGetEndpoints(t *testing.T) {
	handler := newTestHandler(t)

	tests := []struct {
		name      string
		path      string
		checkBody func(*testing.T, map[string]any)
	}{
		{
			name: "health",
			path: "/healthz",
			checkBody: func(t *testing.T, body map[string]any) {
				t.Helper()
				if body["status"] != "ok" {
					t.Errorf("status = %v, want ok", body["status"])
				}
			},
		},
		{
			name: "readiness",
			path: "/readyz",
			checkBody: func(t *testing.T, body map[string]any) {
				t.Helper()
				if body["status"] != "ready" {
					t.Errorf("status = %v, want ready", body["status"])
				}
				checks, ok := body["checks"].(map[string]any)
				if !ok || len(checks) != 0 {
					t.Errorf("checks = %#v, want an empty object", body["checks"])
				}
			},
		},
		{
			name: "service info",
			path: "/api/v1/system/info",
			checkBody: func(t *testing.T, body map[string]any) {
				t.Helper()
				if body["name"] != "main-server" {
					t.Errorf("name = %v, want main-server", body["name"])
				}
				if body["version"] != serviceVersion {
					t.Errorf("version = %v, want %s", body["version"], serviceVersion)
				}
				if body["environment"] != "development" {
					t.Errorf("environment = %v, want development", body["environment"])
				}
				if value, ok := body["time"].(string); !ok {
					t.Error("time is not a string")
				} else if _, err := time.Parse(time.RFC3339Nano, value); err != nil {
					t.Errorf("time %q is not RFC3339: %v", value, err)
				}
			},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodGet, test.path, nil)
			request.Header.Set(requestIDHeader, "test-request-id")
			recorder := httptest.NewRecorder()

			handler.ServeHTTP(recorder, request)

			if recorder.Code != http.StatusOK {
				t.Fatalf("status = %d, want %d", recorder.Code, http.StatusOK)
			}
			if contentType := recorder.Header().Get("Content-Type"); !strings.HasPrefix(contentType, "application/json") {
				t.Errorf("Content-Type = %q, want application/json", contentType)
			}
			if requestID := recorder.Header().Get(requestIDHeader); requestID != "test-request-id" {
				t.Errorf("X-Request-ID = %q, want test-request-id", requestID)
			}

			var body map[string]any
			if err := json.NewDecoder(recorder.Body).Decode(&body); err != nil {
				t.Fatalf("decode response: %v", err)
			}
			test.checkBody(t, body)
		})
	}
}

func TestNotFoundReturnsJSON(t *testing.T) {
	handler := newTestHandler(t)
	request := httptest.NewRequest(http.MethodGet, "/missing", nil)
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusNotFound)
	}
	assertErrorCode(t, recorder, "not_found")
}

func TestMethodNotAllowedReturnsJSON(t *testing.T) {
	handler := newTestHandler(t)
	request := httptest.NewRequest(http.MethodPost, "/healthz", nil)
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusMethodNotAllowed)
	}
	if allow := recorder.Header().Get("Allow"); allow != "GET, HEAD" {
		t.Errorf("Allow = %q, want GET, HEAD", allow)
	}
	assertErrorCode(t, recorder, "method_not_allowed")
}

func TestGeneratedRequestID(t *testing.T) {
	handler := newTestHandler(t)
	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, request)

	if requestID := recorder.Header().Get(requestIDHeader); requestID == "" {
		t.Error("X-Request-ID is empty")
	}
}

func TestRecoveryReturnsJSON(t *testing.T) {
	s := newServer(testConfig(t), discardLogger())
	handler := s.withRequestID(s.withRecovery(http.HandlerFunc(func(
		http.ResponseWriter,
		*http.Request,
	) {
		panic("test panic")
	})))
	request := httptest.NewRequest(http.MethodGet, "/panic", nil)
	request.Header.Set(requestIDHeader, "panic-request")
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", recorder.Code)
	}
	if requestID := recorder.Header().Get(requestIDHeader); requestID != "panic-request" {
		t.Errorf("X-Request-ID = %q, want panic-request", requestID)
	}
	assertErrorCode(t, recorder, "internal_error")
}

func newTestHandler(t *testing.T) http.Handler {
	t.Helper()
	return newServer(testConfig(t), discardLogger()).handler()
}

func testConfig(t *testing.T) config {
	t.Helper()
	computeURL, err := url.Parse("http://127.0.0.1:1/api/v1/chat")
	if err != nil {
		t.Fatalf("parse test compute URL: %v", err)
	}
	return config{
		Addr:                defaultAddr,
		Environment:         "development",
		LogLevel:            slog.LevelDebug,
		ShutdownTimeout:     time.Second,
		ComputeURL:          computeURL,
		ComputeAssistantURL: mustParseURL(t, "http://127.0.0.1:1/api/v1"),
		ComputeTimeout:      time.Second,
	}
}

func mustParseURL(t *testing.T, value string) *url.URL {
	t.Helper()
	parsed, err := url.Parse(value)
	if err != nil {
		t.Fatalf("parse URL %q: %v", value, err)
	}
	return parsed
}

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func loadConfigFromJSON(t *testing.T, body string) config {
	t.Helper()
	cfg, err := loadConfigFromText(t, body)
	if err != nil {
		t.Fatalf("loadConfig() error = %v", err)
	}
	return cfg
}

func loadConfigFromText(t *testing.T, body string) (config, error) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	return loadConfig(path)
}

func assertErrorCode(t *testing.T, recorder *httptest.ResponseRecorder, expected string) {
	t.Helper()

	if contentType := recorder.Header().Get("Content-Type"); !strings.HasPrefix(contentType, "application/json") {
		t.Errorf("Content-Type = %q, want application/json", contentType)
	}

	var body struct {
		Code string `json:"code"`
	}
	if err := json.NewDecoder(recorder.Body).Decode(&body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if body.Code != expected {
		t.Errorf("code = %q, want %q", body.Code, expected)
	}
}
