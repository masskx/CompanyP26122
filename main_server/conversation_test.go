package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestAssistantSessionProxyRoutes(t *testing.T) {
	tests := []struct {
		name          string
		method        string
		frontendPath  string
		body          string
		upstreamPath  string
		upstreamQuery string
		status        int
		response      string
		wantResponse  string
	}{
		{
			name: "list", method: http.MethodGet,
			frontendPath: "/api/v1/assistant/sessions?limit=20&offset=40&ignored=1",
			upstreamPath: "/api/v1/sessions", upstreamQuery: "limit=20&offset=40",
			status: http.StatusOK, response: `[{"session_id":"s_1","title":"报表"}]`,
			wantResponse: `[{"session_id":"s_1","title":"报表"}]`,
		},
		{
			name: "create", method: http.MethodPost,
			frontendPath: "/api/v1/assistant/sessions", body: `{"title":"新对话"}`,
			upstreamPath: "/api/v1/sessions", status: http.StatusCreated,
			response:     `{"session_id":"s_1","title":"新对话"}`,
			wantResponse: `{"session_id":"s_1","title":"新对话"}`,
		},
		{
			name: "create without body", method: http.MethodPost,
			frontendPath: "/api/v1/assistant/sessions",
			upstreamPath: "/api/v1/sessions", status: http.StatusCreated,
			response:     `{"session_id":"s_2"}`,
			wantResponse: `{"session_id":"s_2"}`,
		},
		{
			name: "get", method: http.MethodGet,
			frontendPath: "/api/v1/assistant/sessions/s_1",
			upstreamPath: "/api/v1/sessions/s_1", status: http.StatusOK,
			response:     `{"session_id":"s_1"}`,
			wantResponse: `{"session_id":"s_1"}`,
		},
		{
			name: "rename", method: http.MethodPatch,
			frontendPath: "/api/v1/assistant/sessions/s_1", body: `{"title":"重命名"}`,
			upstreamPath: "/api/v1/sessions/s_1", status: http.StatusOK,
			response:     `{"session_id":"s_1","title":"重命名"}`,
			wantResponse: `{"session_id":"s_1","title":"重命名"}`,
		},
		{
			name: "delete", method: http.MethodDelete,
			frontendPath: "/api/v1/assistant/sessions/s_1",
			upstreamPath: "/api/v1/sessions/s_1", status: http.StatusNoContent,
		},
		{
			name: "messages", method: http.MethodGet,
			frontendPath:  "/api/v1/assistant/sessions/s_1/messages?limit=50&before_seq=80&ignored=1",
			upstreamPath:  "/api/v1/sessions/s_1/messages",
			upstreamQuery: "before_seq=80&limit=50", status: http.StatusOK,
			response:     `[{"message_id":"m_1","seq":79}]`,
			wantResponse: `[{"message_id":"m_1","seq":79}]`,
		},
		{
			name: "upstream error", method: http.MethodGet,
			frontendPath: "/api/v1/assistant/sessions/missing",
			upstreamPath: "/api/v1/sessions/missing", status: http.StatusNotFound,
			response:     `{"detail":"session not found"}`,
			wantResponse: `{"detail":"session not found"}`,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != test.method {
					t.Errorf("upstream method = %s, want %s", r.Method, test.method)
				}
				if r.URL.Path != test.upstreamPath {
					t.Errorf("upstream path = %q, want %q", r.URL.Path, test.upstreamPath)
				}
				if r.URL.RawQuery != test.upstreamQuery {
					t.Errorf("upstream query = %q, want %q", r.URL.RawQuery, test.upstreamQuery)
				}
				for _, header := range []string{"X-User-Id", "X-User-Roles", "X-Client-Id"} {
					if value := r.Header.Get(header); value != "" {
						t.Errorf("upstream %s = %q, want empty", header, value)
					}
				}
				if value := r.Header.Get("X-API-Key"); value != "server-key" {
					t.Errorf("upstream X-API-Key = %q, want server-key", value)
				}
				if value := r.Header.Get(requestIDHeader); value != "test-request" {
					t.Errorf("upstream X-Request-ID = %q, want test-request", value)
				}
				if test.body != "" {
					body, err := io.ReadAll(r.Body)
					if err != nil {
						t.Errorf("read upstream body: %v", err)
					}
					if string(body) != test.body {
						t.Errorf("upstream body = %q, want %q", body, test.body)
					}
				}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(test.status)
				if test.response != "" {
					_, _ = io.WriteString(w, test.response)
				}
			}))
			defer upstream.Close()

			handler := newAssistantTestHandler(
				t, upstream.URL, "/api/v1/chat", "server-key", time.Second,
			)
			request := httptest.NewRequest(test.method, test.frontendPath, strings.NewReader(test.body))
			request.Header.Set("X-Request-ID", "test-request")
			request.Header.Set("X-User-Id", "alice")
			request.Header.Set("X-User-Roles", "admin")
			request.Header.Set("X-Client-Id", "browser-a")
			request.Header.Set("X-API-Key", "browser-key-must-not-win")
			recorder := httptest.NewRecorder()

			handler.ServeHTTP(recorder, request)

			if recorder.Code != test.status {
				t.Fatalf("status = %d, want %d; body=%s",
					recorder.Code, test.status, recorder.Body.String())
			}
			if recorder.Body.String() != test.wantResponse {
				t.Errorf("body = %q, want %q", recorder.Body.String(), test.wantResponse)
			}
		})
	}
}

func TestAssistantSessionProxyRejectsInvalidRequests(t *testing.T) {
	handler := newAssistantTestHandler(
		t, "http://127.0.0.1:1", "/api/v1/chat", "", time.Second,
	)

	tests := []struct {
		name   string
		method string
		path   string
		body   string
		status int
	}{
		{
			name: "method not allowed", method: http.MethodPut,
			path: "/api/v1/assistant/sessions", status: http.StatusMethodNotAllowed,
		},
		{
			name: "invalid session", method: http.MethodGet,
			path: "/api/v1/assistant/sessions/bad%20id", status: http.StatusBadRequest,
		},
		{
			name: "body too large", method: http.MethodPost,
			path:   "/api/v1/assistant/sessions",
			body:   `{"title":"` + strings.Repeat("x", conversationMaxBodyBytes) + `"}`,
			status: http.StatusRequestEntityTooLarge,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest(test.method, test.path, strings.NewReader(test.body))
			recorder := httptest.NewRecorder()
			handler.ServeHTTP(recorder, request)
			if recorder.Code != test.status {
				t.Fatalf("status = %d, want %d; body=%s",
					recorder.Code, test.status, recorder.Body.String())
			}
		})
	}
}
