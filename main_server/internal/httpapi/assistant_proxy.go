package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	assistantChatPath        = "api/v1/chat"
	assistantMaxBodyBytes    = 64 * 1024
	assistantMaxMessageRunes = 4000
	assistantMaxSessionRunes = 128
	upstreamErrorBodyBytes   = 64 * 1024
)

type assistantProxy struct {
	endpoint *url.URL
	client   *http.Client
	timeout  time.Duration
	apiKey   string
	logger   *slog.Logger
}

type assistantChatRequest struct {
	Message   string  `json:"message"`
	SessionID *string `json:"session_id,omitempty"`
}

type assistantUpstreamRequest struct {
	Message   string `json:"message"`
	SessionID string `json:"session_id,omitempty"`
	Stream    bool   `json:"stream"`
}

func newAssistantProxy(options Options, logger *slog.Logger) (*assistantProxy, error) {
	baseURL, err := url.Parse(options.ComputeServiceBaseURL)
	if err != nil {
		return nil, fmt.Errorf("parse compute service base URL: %w", err)
	}
	if baseURL.Scheme != "http" && baseURL.Scheme != "https" {
		return nil, fmt.Errorf("compute service base URL must use http or https")
	}
	if baseURL.Host == "" {
		return nil, fmt.Errorf("compute service base URL must include a host")
	}
	if options.ComputeServiceTimeout <= 0 {
		return nil, fmt.Errorf("compute service timeout must be greater than zero")
	}

	return &assistantProxy{
		endpoint: baseURL.JoinPath(assistantChatPath),
		client:   &http.Client{},
		timeout:  options.ComputeServiceTimeout,
		apiKey:   strings.TrimSpace(options.ComputeServiceAPIKey),
		logger:   logger,
	}, nil
}

func (a *api) handleAssistantChat(w http.ResponseWriter, r *http.Request) {
	if a.assistant == nil {
		writeJSON(w, http.StatusServiceUnavailable, errorResponse{
			Code:      "assistant_not_configured",
			Message:   "assistant service is not configured",
			RequestID: requestIDFromContext(r.Context()),
		})
		return
	}
	a.assistant.serve(w, r)
}

func (p *assistantProxy) serve(w http.ResponseWriter, r *http.Request) {
	requestID := requestIDFromContext(r.Context())
	request, err := decodeAssistantRequest(w, r)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, errorResponse{
			Code:      "assistant_invalid_request",
			Message:   err.Error(),
			RequestID: requestID,
		})
		return
	}

	payload, err := json.Marshal(assistantUpstreamRequest{
		Message:   request.Message,
		SessionID: stringValue(request.SessionID),
		Stream:    true,
	})
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorResponse{
			Code:      "assistant_request_failed",
			Message:   "could not prepare assistant request",
			RequestID: requestID,
		})
		return
	}

	// SSE can legitimately outlive the normal 10-second write timeout.
	controller := http.NewResponseController(w)
	if err := controller.SetWriteDeadline(time.Time{}); err != nil {
		p.logger.Debug("could not clear assistant response write deadline", "error", err)
	}

	ctx, cancel := context.WithTimeout(r.Context(), p.timeout)
	defer cancel()

	upstreamRequest, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		p.endpoint.String(),
		bytes.NewReader(payload),
	)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, errorResponse{
			Code:      "assistant_upstream_unavailable",
			Message:   "could not create assistant upstream request",
			RequestID: requestID,
		})
		return
	}
	upstreamRequest.Header.Set("Accept", "text/event-stream")
	upstreamRequest.Header.Set("Content-Type", "application/json; charset=utf-8")
	upstreamRequest.Header.Set(requestIDHeader, requestID)
	if userID := strings.TrimSpace(r.Header.Get("X-User-Id")); userID != "" {
		upstreamRequest.Header.Set("X-User-Id", userID)
	}
	if p.apiKey != "" {
		upstreamRequest.Header.Set("X-API-Key", p.apiKey)
	}

	upstream, err := p.client.Do(upstreamRequest)
	if err != nil {
		if r.Context().Err() != nil {
			return
		}
		code, status := "assistant_upstream_unavailable", http.StatusBadGateway
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			code, status = "assistant_upstream_timeout", http.StatusGatewayTimeout
		}
		p.logger.Error(
			"assistant upstream request failed",
			"request_id", requestID,
			"code", code,
			"error", err,
		)
		writeJSON(w, status, errorResponse{
			Code:      code,
			Message:   "assistant service is unavailable",
			RequestID: requestID,
		})
		return
	}
	defer upstream.Body.Close()

	if upstream.StatusCode < http.StatusOK || upstream.StatusCode >= http.StatusMultipleChoices {
		writeUpstreamError(w, r, upstream)
		return
	}

	copyAssistantHeaders(w.Header(), upstream.Header)
	status := upstream.StatusCode
	if status < http.StatusOK || status > 299 {
		status = http.StatusOK
	}

	buffer := make([]byte, 32*1024)
	wroteHeader := false
	for {
		read, readErr := upstream.Body.Read(buffer)
		if read > 0 {
			if !wroteHeader {
				w.WriteHeader(status)
				wroteHeader = true
			}
			if _, writeErr := w.Write(buffer[:read]); writeErr != nil {
				return
			}
			_ = controller.Flush()
		}

		if readErr == nil {
			continue
		}
		if errors.Is(readErr, io.EOF) {
			if !wroteHeader {
				w.WriteHeader(status)
			}
			return
		}
		if r.Context().Err() != nil {
			return
		}
		if !wroteHeader {
			code, message := "assistant_stream_failed", "assistant response stream failed"
			if errors.Is(ctx.Err(), context.DeadlineExceeded) {
				code = "assistant_upstream_timeout"
				message = "assistant response timed out"
			}
			writeJSON(w, http.StatusBadGateway, errorResponse{
				Code:      code,
				Message:   message,
				RequestID: requestID,
			})
			return
		}
		code, message := "assistant_stream_failed", "assistant response stream was interrupted"
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			code, message = "assistant_upstream_timeout", "assistant response timed out"
		}
		writeSSEError(w, requestID, code, message)
		return
	}
}

func decodeAssistantRequest(w http.ResponseWriter, r *http.Request) (assistantChatRequest, error) {
	r.Body = http.MaxBytesReader(w, r.Body, assistantMaxBodyBytes)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()

	var request assistantChatRequest
	if err := decoder.Decode(&request); err != nil {
		var maxBytesError *http.MaxBytesError
		if errors.As(err, &maxBytesError) {
			return assistantChatRequest{}, fmt.Errorf("request body is too large")
		}
		return assistantChatRequest{}, fmt.Errorf("request body must be valid JSON")
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return assistantChatRequest{}, fmt.Errorf("request body must contain one JSON object")
	}

	request.Message = strings.TrimSpace(request.Message)
	runes := utf8.RuneCountInString(request.Message)
	if runes == 0 || runes > assistantMaxMessageRunes {
		return assistantChatRequest{}, fmt.Errorf(
			"message must be 1-%d characters",
			assistantMaxMessageRunes,
		)
	}
	if request.SessionID != nil {
		sessionID := strings.TrimSpace(*request.SessionID)
		if sessionID == "" {
			request.SessionID = nil
		} else {
			if utf8.RuneCountInString(sessionID) > assistantMaxSessionRunes {
				return assistantChatRequest{}, fmt.Errorf("session_id is too long")
			}
			request.SessionID = &sessionID
		}
	}
	return request, nil
}

func copyAssistantHeaders(destination, source http.Header) {
	destination.Set("Content-Type", "text/event-stream; charset=utf-8")
	destination.Set("Cache-Control", "no-cache")
	destination.Set("X-Accel-Buffering", "no")
	destination.Set("X-Content-Type-Options", "nosniff")
	if value := source.Get("Content-Type"); value != "" {
		destination.Set("Content-Type", value)
	}
	if value := source.Get("Cache-Control"); value != "" {
		destination.Set("Cache-Control", value)
	}
	if value := source.Get("X-Session-Id"); value != "" {
		destination.Set("X-Session-Id", value)
	}
}

func writeUpstreamError(w http.ResponseWriter, r *http.Request, response *http.Response) {
	body, _ := io.ReadAll(io.LimitReader(response.Body, upstreamErrorBodyBytes))
	code, message := upstreamErrorDetails(body)
	status := response.StatusCode
	if status < http.StatusBadRequest || status > 599 {
		status = http.StatusBadGateway
	}
	if message == "" {
		message = fmt.Sprintf("assistant service returned HTTP %d", response.StatusCode)
	}
	if code == "" {
		code = "assistant_upstream_error"
	}
	writeJSON(w, status, errorResponse{
		Code:      code,
		Message:   message,
		RequestID: requestIDFromContext(r.Context()),
	})
}

func upstreamErrorDetails(body []byte) (string, string) {
	var payload struct {
		Code    string          `json:"code"`
		Message string          `json:"message"`
		Detail  json.RawMessage `json:"detail"`
	}
	if json.Unmarshal(body, &payload) != nil {
		return "", ""
	}
	if payload.Code != "" || payload.Message != "" {
		return payload.Code, payload.Message
	}
	if len(payload.Detail) == 0 {
		return "", ""
	}

	var message string
	if json.Unmarshal(payload.Detail, &message) == nil {
		return "", message
	}

	var detail struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	}
	if json.Unmarshal(payload.Detail, &detail) == nil {
		return detail.Code, detail.Message
	}
	return "", ""
}

func writeSSEError(w io.Writer, requestID, code, message string) {
	payload, _ := json.Marshal(errorResponse{
		Code:      code,
		Message:   message,
		RequestID: requestID,
	})
	_, _ = fmt.Fprintf(w, "event: error\ndata: %s\n\n", payload)
}

func stringValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}
