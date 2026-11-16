// Package main starts the MES main-server HTTP gateway.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

const serviceVersion = "0.1.0"

func main() {
	if err := run(); err != nil {
		slog.Error("main server stopped with an error", "error", err)
		os.Exit(1)
	}
}

func run() error {
	configPath := flag.String(
		"config",
		defaultConfigPath,
		"path to the JSON configuration file",
	)
	flag.Parse()

	cfg, err := loadConfig(*configPath)
	if err != nil {
		return fmt.Errorf("load configuration: %w", err)
	}

	logger := newLogger(cfg)
	slog.SetDefault(logger)

	handler := newServer(cfg, logger).handler()

	server := &http.Server{
		Addr:              cfg.Addr,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	serverErrors := make(chan error, 1)
	go func() {
		logger.Info("http server listening",
			"address", cfg.Addr,
			"environment", cfg.Environment,
			"version", serviceVersion,
		)
		serverErrors <- server.ListenAndServe()
	}()

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(signals)

	select {
	case err := <-serverErrors:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return fmt.Errorf("serve HTTP: %w", err)

	case received := <-signals:
		logger.Info("shutdown signal received", "signal", received.String())

		ctx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
		defer cancel()

		if err := server.Shutdown(ctx); err != nil {
			_ = server.Close()
			return fmt.Errorf("graceful shutdown: %w", err)
		}

		if err := <-serverErrors; err != nil && !errors.Is(err, http.ErrServerClosed) {
			return fmt.Errorf("serve HTTP during shutdown: %w", err)
		}

		logger.Info("http server stopped")
		return nil
	}
}

func newLogger(cfg config) *slog.Logger {
	options := &slog.HandlerOptions{Level: cfg.LogLevel}

	var handler slog.Handler
	if cfg.Environment == "production" {
		handler = slog.NewJSONHandler(os.Stdout, options)
	} else {
		handler = slog.NewTextHandler(os.Stdout, options)
	}

	return slog.New(handler).With("service", "main-server")
}
