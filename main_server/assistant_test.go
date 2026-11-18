package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path"
	"strings"
	"testing"
	"time"
)

func TestAssistantChatProxiesSSE(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("upstream method = %s, want POST", r.Method)
		}
		if r.URL.Path != "/custom/chat" {
			t.Errorf("upstream path = %s, want /custom/chat", r.URL.Path)
		}
		if value := r.Header.Get(requestIDHeader); value != "client-request" {
			t.Errorf("upstream X-Request-ID = %q, want client-request", value)
		}
		if value := r.Header.Get("X-User-Id"); value != "" {
			t.Errorf("upstream X-User-Id = %q, want empty", value)
		}
		if value := r.Header.Get("X-User-Roles"); value != "" {
			t.Errorf("upstream X-User-Roles = %q, want empty", value)
		}
		if value := r.Header.Get("X-Client-Id"); value != "" {
			t.Errorf("upstream X-Client-Id = %q, want empty", value)
		}
		if value := r.Header.Get("X-API-Key"); value != "server-key" {
			t.Errorf("upstream X-API-Key = %q, want server-key", value)
		}

		var body struct {
			Message   string `json:"message"`
			SessionID string `json:"session_id"`
			Stream    bool   `json:"stream"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatalf("decode upstream body: %v", err)
		}
		if body.Message != "你好" || body.SessionID != "s_existing" || !body.Stream {
			t.Errorf("upstream body = %#v, want trimmed message, session and stream=true", body)
		}

		w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
		w.Header().Set("X-Session-Id", "s_existing")
		_, _ = io.WriteString(w, "event: session\ndata: {\"session_id\":\"s_existing\"}\n\n")
		_, _ = io.WriteString(w, "event: delta\ndata: {\"text\":\"你\"}\n\n")
		_, _ = io.WriteString(w, "event: done\ndata: {\"message\":{\"content\":\"你好\"}}\n\n")
	}))
	defer upstream.Close()

	handler := newAssistantTestHandler(
		t,
		upstream.URL,
		"/custom/chat",
		"server-key",
		3*time.Second,
	)
	request := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/assistant/chat",
		strings.NewReader(`{"message":"  你好  ","session_id":"s_existing"}`),
	)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Request-ID", "client-request")
	request.Header.Set("X-User-Id", "alice")
	request.Header.Set("X-User-Roles", "admin")
	request.Header.Set("X-Client-Id", "browser-a")
	request.Header.Set("X-API-Key", "browser-key-must-not-win")
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", recorder.Code, recorder.Body.String())
	}
	if value := recorder.Header().Get("Content-Type"); !strings.HasPrefix(value, "text/event-stream") {
		t.Errorf("Content-Type = %q, want text/event-stream", value)
	}
	if value := recorder.Header().Get("X-Session-Id"); value != "s_existing" {
		t.Errorf("X-Session-Id = %q, want s_existing", value)
	}
	if value := recorder.Header().Get("X-Accel-Buffering"); value != "no" {
		t.Errorf("X-Accel-Buffering = %q, want no", value)
	}
	for _, event := range []string{"event: session", "event: delta", "event: done"} {
		if !strings.Contains(recorder.Body.String(), event) {
			t.Errorf("response body does not contain %q: %s", event, recorder.Body.String())
		}
	}
}

func TestAssistantChatOmitsEmptyAPIKey(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if value := r.Header.Get("X-API-Key"); value != "" {
			t.Errorf("upstream X-API-Key = %q, want empty", value)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "event: done\ndata: {\"message\":{\"content\":\"ok\"}}\n\n")
	}))
	defer upstream.Close()

	handler := newAssistantTestHandler(
		t,
		upstream.URL,
		"/api/v1/chat",
		"",
		time.Second,
	)
	request := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/assistant/chat",
		strings.NewReader(`{"message":"你好"}`),
	)
	request.Header.Set("X-API-Key", "browser-key-must-not-be-forwarded")
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestAssistantChatMapsUpstreamError(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(w, `{"detail":{"code":"llm_not_configured","message":"LLM_PROVIDER is none"}}`)
	}))
	defer upstream.Close()

	handler := newAssistantTestHandler(
		t,
		upstream.URL,
		"/api/v1/chat",
		"",
		time.Second,
	)
	request := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/assistant/chat",
		strings.NewReader(`{"message":"你好"}`),
	)
	request.Header.Set("X-Request-ID", "test-request")
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", recorder.Code)
	}
	var body struct {
		Code      string `json:"code"`
		Message   string `json:"message"`
		RequestID string `json:"request_id"`
	}
	if err := json.NewDecoder(recorder.Body).Decode(&body); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if body.Code != "llm_not_configured" || body.Message != "LLM_PROVIDER is none" {
		t.Errorf("error body = %#v", body)
	}
	if body.RequestID != "test-request" {
		t.Errorf("request_id = %q, want test-request", body.RequestID)
	}
}

func TestAssistantChatRejectsInvalidRequests(t *testing.T) {
	handler := newAssistantTestHandler(
		t,
		"http://127.0.0.1:1",
		"/api/v1/chat",
		"",
		time.Second,
	)
	tests := []struct {
		name string
		body string
	}{
		{name: "invalid JSON", body: `{"message":`},
		{name: "empty message", body: `{"message":"   "}`},
		{name: "unknown field", body: `{"message":"你好","extra":true}`},
		{name: "multiple objects", body: `{"message":"你好"}{"message":"再见"}`},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest(
				http.MethodPost,
				"/api/v1/assistant/chat",
				strings.NewReader(test.body),
			)
			recorder := httptest.NewRecorder()

			handler.ServeHTTP(recorder, request)

			if recorder.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400; body=%s", recorder.Code, recorder.Body.String())
			}
			assertErrorCode(t, recorder, "assistant_invalid_request")
		})
	}
}

func TestAssistantChatReportsUnavailableAndTimeout(t *testing.T) {
	t.Run("unavailable", func(t *testing.T) {
		upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
		upstreamURL := upstream.URL
		upstream.Close()

		handler := newAssistantTestHandler(
			t,
			upstreamURL,
			"/api/v1/chat",
			"",
			time.Second,
		)
		recorder := serveAssistantRequest(handler, `{"message":"你好"}`)

		if recorder.Code != http.StatusBadGateway {
			t.Fatalf("status = %d, want 502; body=%s", recorder.Code, recorder.Body.String())
		}
		assertErrorCode(t, recorder, "assistant_upstream_unavailable")
	})

	t.Run("timeout", func(t *testing.T) {
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			select {
			case <-r.Context().Done():
			case <-time.After(time.Second):
			}
		}))
		defer upstream.Close()

		handler := newAssistantTestHandler(
			t,
			upstream.URL,
			"/api/v1/chat",
			"",
			20*time.Millisecond,
		)
		recorder := serveAssistantRequest(handler, `{"message":"你好"}`)

		if recorder.Code != http.StatusGatewayTimeout {
			t.Fatalf("status = %d, want 504; body=%s", recorder.Code, recorder.Body.String())
		}
		assertErrorCode(t, recorder, "assistant_upstream_timeout")
	})
}

func TestAssistantChatMethodNotAllowed(t *testing.T) {
	handler := newAssistantTestHandler(
		t,
		"http://127.0.0.1:1",
		"/api/v1/chat",
		"",
		time.Second,
	)
	request := httptest.NewRequest(http.MethodGet, "/api/v1/assistant/chat", nil)
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, request)

	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want 405", recorder.Code)
	}
	if allow := recorder.Header().Get("Allow"); allow != "POST" {
		t.Errorf("Allow = %q, want POST", allow)
	}
}

func TestUpstreamErrorDetails(t *testing.T) {
	tests := []struct {
		name        string
		body        string
		wantCode    string
		wantMessage string
	}{
		{
			name:        "detail object",
			body:        `{"detail":{"code":"session_not_found","message":"missing"}}`,
			wantCode:    "session_not_found",
			wantMessage: "missing",
		},
		{
			name:        "detail string",
			body:        `{"detail":"invalid X-User-Id"}`,
			wantMessage: "invalid X-User-Id",
		},
		{
			name:        "flat object",
			body:        `{"code":"agent_failed","message":"model failed"}`,
			wantCode:    "agent_failed",
			wantMessage: "model failed",
		},
		{
			name: "plain text",
			body: "bad gateway",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			code, message := upstreamErrorDetails([]byte(test.body))
			if code != test.wantCode || message != test.wantMessage {
				t.Errorf("got (%q, %q), want (%q, %q)",
					code, message, test.wantCode, test.wantMessage)
			}
		})
	}
}

func TestWriteSSEError(t *testing.T) {
	var body strings.Builder
	writeSSEError(&body, "request-1", "assistant_stream_failed", "broken")
	expected := fmt.Sprintf(
		"event: error\ndata: {\"code\":\"assistant_stream_failed\",\"message\":\"broken\",\"request_id\":\"request-1\"}\n\n",
	)
	if body.String() != expected {
		t.Errorf("SSE error = %q, want %q", body.String(), expected)
	}
}

func newAssistantTestHandler(
	t *testing.T,
	baseURL string,
	chatPath string,
	apiKey string,
	timeout time.Duration,
) http.Handler {
	t.Helper()
	base, err := url.Parse(strings.TrimRight(baseURL, "/"))
	if err != nil {
		t.Fatalf("parse test compute URL: %v", err)
	}
	cfg := config{
		Addr:                defaultAddr,
		Environment:         "test",
		ComputeURL:          base.JoinPath(strings.TrimPrefix(chatPath, "/")),
		ComputeAssistantURL: base.JoinPath(strings.TrimPrefix(path.Dir(chatPath), "/")),
		ComputeTimeout:      timeout,
		ComputeAPIKey:       apiKey,
		ShutdownTimeout:     time.Second,
	}
	return newServer(cfg, discardLogger()).handler()
}

func serveAssistantRequest(handler http.Handler, body string) *httptest.ResponseRecorder {
	request := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/assistant/chat",
		strings.NewReader(body),
	)
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	return recorder
}
