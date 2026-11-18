package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
)

const conversationMaxBodyBytes = 64 * 1024

var sessionIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

func (s *server) registerAssistantSessionRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/assistant/sessions", s.handleSessionList)
	mux.HandleFunc("POST /api/v1/assistant/sessions", s.handleSessionCreate)
	mux.HandleFunc("/api/v1/assistant/sessions", methodNotAllowed("GET, POST"))

	mux.HandleFunc("GET /api/v1/assistant/sessions/{session_id}", s.handleSessionGet)
	mux.HandleFunc("PATCH /api/v1/assistant/sessions/{session_id}", s.handleSessionRename)
	mux.HandleFunc("DELETE /api/v1/assistant/sessions/{session_id}", s.handleSessionDelete)
	mux.HandleFunc("/api/v1/assistant/sessions/{session_id}",
		methodNotAllowed("GET, PATCH, DELETE"))

	mux.HandleFunc("GET /api/v1/assistant/sessions/{session_id}/messages",
		s.handleSessionMessages)
	mux.HandleFunc("/api/v1/assistant/sessions/{session_id}/messages",
		methodNotAllowed("GET"))
}

func (s *server) handleSessionList(w http.ResponseWriter, r *http.Request) {
	s.proxyConversation(w, r, []string{"sessions"},
		allowedQuery(r.URL.Query(), "limit", "offset"), false)
}

func (s *server) handleSessionCreate(w http.ResponseWriter, r *http.Request) {
	s.proxyConversation(w, r, []string{"sessions"}, nil, true)
}

func (s *server) handleSessionGet(w http.ResponseWriter, r *http.Request) {
	sessionID, ok := conversationSessionID(w, r)
	if !ok {
		return
	}
	s.proxyConversation(w, r, []string{"sessions", sessionID}, nil, false)
}

func (s *server) handleSessionRename(w http.ResponseWriter, r *http.Request) {
	sessionID, ok := conversationSessionID(w, r)
	if !ok {
		return
	}
	s.proxyConversation(w, r, []string{"sessions", sessionID}, nil, true)
}

func (s *server) handleSessionDelete(w http.ResponseWriter, r *http.Request) {
	sessionID, ok := conversationSessionID(w, r)
	if !ok {
		return
	}
	s.proxyConversation(w, r, []string{"sessions", sessionID}, nil, false)
}

func (s *server) handleSessionMessages(w http.ResponseWriter, r *http.Request) {
	sessionID, ok := conversationSessionID(w, r)
	if !ok {
		return
	}
	s.proxyConversation(w, r,
		[]string{"sessions", sessionID, "messages"},
		allowedQuery(r.URL.Query(), "limit", "before_seq"), false)
}

func (s *server) proxyConversation(
	w http.ResponseWriter,
	r *http.Request,
	path []string,
	query url.Values,
	withBody bool,
) {
	requestID := requestIDFromContext(r.Context())
	if s.computeAssistantURL == nil {
		writeJSON(w, http.StatusBadGateway, errorResponse{
			Code:      "assistant_upstream_unavailable",
			Message:   "assistant session service is not configured",
			RequestID: requestID,
		})
		return
	}

	var body []byte
	if withBody {
		var err error
		body, err = readConversationBody(w, r)
		if err != nil {
			code := "assistant_invalid_request"
			status := http.StatusBadRequest
			if errors.Is(err, errConversationBodyTooLarge) {
				status = http.StatusRequestEntityTooLarge
			}
			writeJSON(w, status, errorResponse{
				Code: code, Message: err.Error(), RequestID: requestID,
			})
			return
		}
		if len(body) == 0 {
			body = nil
		}
	}

	target := s.computeAssistantURL.JoinPath(path...)
	if query != nil {
		target.RawQuery = query.Encode()
	}
	ctx, cancel := context.WithTimeout(r.Context(), s.timeout)
	defer cancel()

	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	upstreamRequest, err := http.NewRequestWithContext(ctx, r.Method, target.String(), reader)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, errorResponse{
			Code:      "assistant_upstream_unavailable",
			Message:   "could not prepare assistant session request",
			RequestID: requestID,
		})
		return
	}
	upstreamRequest.Header.Set("Accept", "application/json")
	upstreamRequest.Header.Set(requestIDHeader, requestID)
	if body != nil {
		upstreamRequest.Header.Set("Content-Type", "application/json; charset=utf-8")
	}
	if s.computeKey != "" {
		upstreamRequest.Header.Set("X-API-Key", s.computeKey)
	}

	upstream, err := s.client.Do(upstreamRequest)
	if err != nil {
		if r.Context().Err() != nil {
			return
		}
		code, status := "assistant_upstream_unavailable", http.StatusBadGateway
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			code, status = "assistant_upstream_timeout", http.StatusGatewayTimeout
		}
		s.logger.Error("assistant session upstream failed",
			"request_id", requestID, "code", code, "error", err)
		writeJSON(w, status, errorResponse{
			Code: code, Message: "assistant session service is unavailable",
			RequestID: requestID,
		})
		return
	}
	defer upstream.Body.Close()

	if contentType := upstream.Header.Get("Content-Type"); contentType != "" {
		w.Header().Set("Content-Type", contentType)
	}
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(upstream.StatusCode)
	if upstream.StatusCode == http.StatusNoContent {
		return
	}
	if _, err := io.Copy(w, upstream.Body); err != nil {
		s.logger.Error("copy assistant session response",
			"request_id", requestID, "error", err)
	}
}

var errConversationBodyTooLarge = errors.New("request body is too large")

func readConversationBody(w http.ResponseWriter, r *http.Request) ([]byte, error) {
	r.Body = http.MaxBytesReader(w, r.Body, conversationMaxBodyBytes)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		var maxBytesError *http.MaxBytesError
		if errors.As(err, &maxBytesError) {
			return nil, errConversationBodyTooLarge
		}
		return nil, fmt.Errorf("request body is invalid")
	}
	if len(body) > conversationMaxBodyBytes {
		return nil, errConversationBodyTooLarge
	}
	return body, nil
}

func conversationSessionID(w http.ResponseWriter, r *http.Request) (string, bool) {
	sessionID := strings.TrimSpace(r.PathValue("session_id"))
	if sessionIDPattern.MatchString(sessionID) {
		return sessionID, true
	}
	writeJSON(w, http.StatusBadRequest, errorResponse{
		Code:      "assistant_invalid_request",
		Message:   "invalid session_id",
		RequestID: requestIDFromContext(r.Context()),
	})
	return "", false
}

func allowedQuery(values url.Values, names ...string) url.Values {
	allowed := make(url.Values, len(names))
	for _, name := range names {
		for _, value := range values[name] {
			allowed.Add(name, value)
		}
	}
	return allowed
}
