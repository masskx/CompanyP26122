package httpapi

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"time"
)

const requestIDHeader = "X-Request-ID"

// Options controls service metadata and the outbound compute-service integration.
type Options struct {
	Version               string
	Environment           string
	ComputeServiceBaseURL string
	ComputeServiceTimeout time.Duration
	ComputeServiceAPIKey  string
}

type api struct {
	version     string
	environment string
	logger      *slog.Logger
	assistant   *assistantProxy
}

type statusResponse struct {
	Status string `json:"status"`
}

type readinessResponse struct {
	Status string            `json:"status"`
	Checks map[string]string `json:"checks"`
}

type serviceInfoResponse struct {
	Name        string `json:"name"`
	Version     string `json:"version"`
	Environment string `json:"environment"`
	Time        string `json:"time"`
}

type errorResponse struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	RequestID string `json:"request_id,omitempty"`
}

// New builds the complete HTTP handler, including middleware.
func New(options Options, logger *slog.Logger) http.Handler {
	if logger == nil {
		logger = slog.Default()
	}

	service := &api{
		version:     options.Version,
		environment: options.Environment,
		logger:      logger,
	}
	if options.ComputeServiceBaseURL != "" {
		assistant, err := newAssistantProxy(options, logger)
		if err != nil {
			logger.Error("assistant proxy is disabled", "error", err)
		} else {
			service.assistant = assistant
		}
	}

	mux := http.NewServeMux()
	registerJSONRoute(mux, "GET /healthz", service.handleHealth)
	registerJSONRoute(mux, "GET /readyz", service.handleReadiness)
	registerJSONRoute(mux, "GET /api/v1/system/info", service.handleInfo)
	if service.assistant != nil {
		mux.HandleFunc("POST /api/v1/assistant/chat", service.handleAssistantChat)
		mux.HandleFunc("/api/v1/assistant/chat", methodNotAllowedWithAllow("POST"))
	}
	mux.HandleFunc("/", service.handleNotFound)

	var handler http.Handler = mux
	handler = service.withAccessLog(handler)
	handler = service.withRecovery(handler)
	handler = service.withRequestID(handler)
	return handler
}

func registerJSONRoute(mux *http.ServeMux, pattern string, handler http.HandlerFunc) {
	mux.HandleFunc(pattern, handler)

	path := pattern
	if separator := indexSpace(pattern); separator >= 0 {
		path = pattern[separator+1:]
	}
	mux.HandleFunc(path, methodNotAllowed)
}

func indexSpace(value string) int {
	for index, character := range value {
		if character == ' ' {
			return index
		}
	}
	return -1
}

func (a *api) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, statusResponse{Status: "ok"})
}

func (a *api) handleReadiness(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, readinessResponse{
		Status: "ready",
		Checks: map[string]string{},
	})
}

func (a *api) handleInfo(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, serviceInfoResponse{
		Name:        "main-server",
		Version:     a.version,
		Environment: a.environment,
		Time:        time.Now().UTC().Format(time.RFC3339Nano),
	})
}

func (a *api) handleNotFound(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusNotFound, errorResponse{
		Code:      "not_found",
		Message:   "route not found",
		RequestID: requestIDFromContext(r.Context()),
	})
}

func methodNotAllowed(w http.ResponseWriter, r *http.Request) {
	methodNotAllowedWithAllow("GET, HEAD")(w, r)
}

func methodNotAllowedWithAllow(allow string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Allow", allow)
		writeJSON(w, http.StatusMethodNotAllowed, errorResponse{
			Code:      "method_not_allowed",
			Message:   "method not allowed",
			RequestID: requestIDFromContext(r.Context()),
		})
	}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
