package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"runtime/debug"
	"strings"
	"time"
)

const requestIDHeader = "X-Request-ID"

type server struct {
	version             string
	environment         string
	logger              *slog.Logger
	computeURL          *url.URL
	computeAssistantURL *url.URL
	computeKey          string
	client              *http.Client
	timeout             time.Duration
}

type errorResponse struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	RequestID string `json:"request_id,omitempty"`
}

func newServer(cfg config, logger *slog.Logger) *server {
	if logger == nil {
		logger = slog.Default()
	}
	return &server{
		version:             serviceVersion,
		environment:         cfg.Environment,
		logger:              logger,
		computeURL:          cfg.ComputeURL,
		computeAssistantURL: cfg.ComputeAssistantURL,
		computeKey:          cfg.ComputeAPIKey,
		client:              &http.Client{},
		timeout:             cfg.ComputeTimeout,
	}
}

func (s *server) handler() http.Handler {
	mux := http.NewServeMux()
	s.get(mux, "/healthz", s.handleHealth)
	s.get(mux, "/readyz", s.handleReadiness)
	s.get(mux, "/api/v1/system/info", s.handleInfo)

	mux.HandleFunc("POST /api/v1/assistant/chat", s.handleAssistantChat)
	mux.HandleFunc("/api/v1/assistant/chat", methodNotAllowed("POST"))
	s.registerAssistantSessionRoutes(mux)
	mux.HandleFunc("/", s.handleNotFound)

	var handler http.Handler = mux
	handler = s.withAccessLog(handler)
	handler = s.withRecovery(handler)
	handler = s.withRequestID(handler)
	return handler
}

func (s *server) get(
	mux *http.ServeMux,
	path string,
	handler http.HandlerFunc,
) {
	mux.HandleFunc("GET "+path, handler)
	mux.HandleFunc(path, methodNotAllowed("GET, HEAD"))
}

func methodNotAllowed(allow string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Allow", allow)
		writeJSON(w, http.StatusMethodNotAllowed, errorResponse{
			Code:      "method_not_allowed",
			Message:   "method not allowed",
			RequestID: requestIDFromContext(r.Context()),
		})
	}
}

func (s *server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *server) handleReadiness(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "ready",
		"checks": map[string]string{},
	})
}

func (s *server) handleInfo(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{
		"name":        "main-server",
		"version":     s.version,
		"environment": s.environment,
		"time":        time.Now().UTC().Format(time.RFC3339Nano),
	})
}

func (s *server) handleNotFound(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusNotFound, errorResponse{
		Code:      "not_found",
		Message:   "route not found",
		RequestID: requestIDFromContext(r.Context()),
	})
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

type requestIDContextKey struct{}

func (s *server) withRequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestID := strings.TrimSpace(r.Header.Get(requestIDHeader))
		if requestID == "" || len(requestID) > 128 {
			requestID = newRequestID()
		}

		r.Header.Set(requestIDHeader, requestID)
		w.Header().Set(requestIDHeader, requestID)

		ctx := context.WithValue(r.Context(), requestIDContextKey{}, requestID)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func (s *server) withRecovery(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if recovered := recover(); recovered != nil {
				requestID := requestIDFromContext(r.Context())
				s.logger.Error("panic recovered",
					"request_id", requestID,
					"method", r.Method,
					"path", r.URL.Path,
					"panic", recovered,
					"stack", string(debug.Stack()),
				)
				writeJSON(w, http.StatusInternalServerError, errorResponse{
					Code:      "internal_error",
					Message:   "internal server error",
					RequestID: requestID,
				})
			}
		}()

		next.ServeHTTP(w, r)
	})
}

func (s *server) withAccessLog(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		recorder := &responseRecorder{
			ResponseWriter: w,
			status:         http.StatusOK,
		}

		defer func() {
			s.logger.Info("http request",
				"request_id", requestIDFromContext(r.Context()),
				"method", r.Method,
				"path", r.URL.Path,
				"status", recorder.status,
				"bytes", recorder.bytes,
				"duration_ms", time.Since(started).Milliseconds(),
				"remote_addr", r.RemoteAddr,
			)
		}()

		next.ServeHTTP(recorder, r)
	})
}

type responseRecorder struct {
	http.ResponseWriter
	status      int
	bytes       int
	wroteHeader bool
}

func (r *responseRecorder) Unwrap() http.ResponseWriter {
	return r.ResponseWriter
}

func (r *responseRecorder) WriteHeader(status int) {
	if r.wroteHeader {
		return
	}
	r.status = status
	r.wroteHeader = true
	r.ResponseWriter.WriteHeader(status)
}

func (r *responseRecorder) Write(body []byte) (int, error) {
	if !r.wroteHeader {
		r.WriteHeader(http.StatusOK)
	}
	written, err := r.ResponseWriter.Write(body)
	r.bytes += written
	return written, err
}

func requestIDFromContext(ctx context.Context) string {
	requestID, _ := ctx.Value(requestIDContextKey{}).(string)
	return requestID
}

func newRequestID() string {
	var randomBytes [16]byte
	if _, err := rand.Read(randomBytes[:]); err == nil {
		return hex.EncodeToString(randomBytes[:])
	}
	return fmt.Sprintf("%d", time.Now().UnixNano())
}
