package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"shop-mesh-lab/internal/store"
)

const maxOrderSize = 1 << 20

type api struct {
	store   *store.Store
	version string
	flaky   bool
	fail    bool
	rate    float64
}

type responseRecorder struct {
	http.ResponseWriter
	status int
}

func (r *responseRecorder) WriteHeader(status int) {
	r.status = status
	r.ResponseWriter.WriteHeader(status)
}

func (r *responseRecorder) Write(body []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	return r.ResponseWriter.Write(body)
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	db, err := store.Open(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		slog.Error("database initialization failed", "error", err)
		os.Exit(1)
	}
	defer db.Close()

	version := envOrDefault("VERSION", "v1")
	app := &api{
		store: db, version: version,
		fail:  version == "v2" && envBool("FAIL"),
		flaky: version == "v1" && envBool("FLAKY"),
		rate:  envFloat("FLAKY_RATE", 0.30),
	}

	requests := prometheus.NewCounterVec(prometheus.CounterOpts{
		Namespace: "shop", Subsystem: "api", Name: "http_requests_total",
		Help: "Total HTTP requests handled by the shop API.",
	}, []string{"method", "route", "status", "version"})
	duration := prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Namespace: "shop", Subsystem: "api", Name: "http_request_duration_seconds",
		Help: "HTTP request latency at the application handler.", Buckets: prometheus.DefBuckets,
	}, []string{"method", "route", "version"})
	prometheus.MustRegister(requests, duration)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", health)
	mux.HandleFunc("GET /version", app.getVersion)
	mux.HandleFunc("POST /order", app.createOrder)
	mux.HandleFunc("GET /orders", app.listOrders)
	mux.Handle("GET /metrics", promhttp.Handler())

	server := &http.Server{
		Addr:              envOrDefault("HTTP_ADDR", ":8080"),
		Handler:           instrument(mux, requests, duration, version),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	go func() {
		slog.Info("api listening", "address", server.Addr, "version", version, "fail", app.fail, "flaky", app.flaky)
		if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("api server failed", "error", err)
			stop()
		}
	}()
	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		slog.Error("api shutdown failed", "error", err)
	}
}

func health(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = w.Write([]byte("ok"))
}

func (a *api) getVersion(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = w.Write([]byte(a.version + "\n"))
}

func (a *api) createOrder(w http.ResponseWriter, r *http.Request) {
	if a.fail || (a.flaky && rand.Float64() < a.rate) {
		writeError(w, http.StatusInternalServerError, "injected release failure")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxOrderSize))
	if err != nil || !json.Valid(body) {
		writeError(w, http.StatusBadRequest, "request body must be valid JSON and at most 1 MiB")
		return
	}
	order, err := a.store.CreateOrder(r.Context(), body)
	if err != nil {
		slog.Error("create order failed", "error", err)
		writeError(w, http.StatusInternalServerError, "could not create order")
		return
	}
	writeJSON(w, http.StatusCreated, order)
}

func (a *api) listOrders(w http.ResponseWriter, r *http.Request) {
	orders, err := a.store.ListOrders(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "could not list orders")
		return
	}
	writeJSON(w, http.StatusOK, orders)
}

func instrument(next http.Handler, requests *prometheus.CounterVec, duration *prometheus.HistogramVec, version string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		recorder := &responseRecorder{ResponseWriter: w}
		next.ServeHTTP(recorder, r)
		status := recorder.status
		if status == 0 {
			status = http.StatusOK
		}
		route := metricRoute(r.URL.Path)
		requests.WithLabelValues(r.Method, route, strconv.Itoa(status), version).Inc()
		duration.WithLabelValues(r.Method, route, version).Observe(time.Since(started).Seconds())
	})
}

func metricRoute(path string) string {
	switch path {
	case "/health", "/version", "/order", "/orders", "/metrics":
		return path
	default:
		return "unmatched"
	}
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}
func envOrDefault(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
func envBool(name string) bool { value, _ := strconv.ParseBool(os.Getenv(name)); return value }
func envFloat(name string, fallback float64) float64 {
	value, err := strconv.ParseFloat(os.Getenv(name), 64)
	if err != nil || value < 0 || value > 1 {
		return fallback
	}
	return value
}
