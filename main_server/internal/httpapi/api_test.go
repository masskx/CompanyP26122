package httpapi

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestGetEndpoints(t *testing.T) {
	handler := newTestHandler()

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
				if body["version"] != "0.1.0" {
					t.Errorf("version = %v, want 0.1.0", body["version"])
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
	handler := newTestHandler()
	request := httptest.NewRequest(http.MethodGet, "/missing", nil)
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusNotFound)
	}
	assertErrorCode(t, recorder, "not_found")
}

func TestMethodNotAllowedReturnsJSON(t *testing.T) {
	handler := newTestHandler()
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
	handler := newTestHandler()
	request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, request)

	if requestID := recorder.Header().Get(requestIDHeader); requestID == "" {
		t.Error("X-Request-ID is empty")
	}
}

func newTestHandler() http.Handler {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	return New(Options{
		Version:     "0.1.0",
		Environment: "development",
	}, logger)
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
