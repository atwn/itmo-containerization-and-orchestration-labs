// batch stands in for "nightly analytics": it burns CPU and memory in a loop
// that produces nothing useful. Its only job in Lab 4 is to be a sacrificial
// background workload that competes with the shop critical path for node
// resources, so the chart's priority/QoS/PDB guardrails have something real
// to protect api/postgres from.
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"syscall"
	"time"

	"shop/internal/health"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	cpuWorkers := intFromEnv("CPU_WORKERS", runtime.NumCPU())
	memoryTargetMB := intFromEnv("MEMORY_MB", 0)
	memoryStepMB := intFromEnv("MEMORY_STEP_MB", 16)
	memoryStepInterval := durationFromEnv("MEMORY_STEP_INTERVAL", 2*time.Second)

	slog.Info("batch starting",
		"cpu_workers", cpuWorkers,
		"memory_target_mb", memoryTargetMB,
		"memory_step_mb", memoryStepMB,
		"memory_step_interval", memoryStepInterval,
	)

	for i := 0; i < cpuWorkers; i++ {
		go burnCPU(ctx)
	}
	go burnMemory(ctx, memoryTargetMB, memoryStepMB, memoryStepInterval)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", health.Handler)
	address := envOrDefault("HTTP_ADDR", ":8080")
	server := &http.Server{
		Addr:              address,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		slog.Info("batch health endpoint listening", "address", address)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("batch health server failed", "error", err)
			stop()
		}
	}()

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = server.Shutdown(shutdownCtx)
}

// burnCPU spins a tight loop so this goroutine is always runnable, giving the
// kernel real CPU demand to schedule and, once the container hits its CPU
// limit, real throttling to observe.
func burnCPU(ctx context.Context) {
	var x uint64
	for {
		select {
		case <-ctx.Done():
			return
		default:
			for i := 0; i < 1_000_000; i++ {
				x = x*2862933555777941757 + 3037000493
			}
		}
	}
}

// burnMemory grows a retained allocation up to memoryTargetMB in steps,
// actually writing every page so RSS reflects it (a Go slice that is only
// make()'d, never written, may not be backed by real pages yet). Growing in
// steps rather than allocating everything at once lets Part 7 of the lab
// raise memoryTargetMB via values.yaml and watch pressure build gradually
// instead of OOMing instantly on startup.
func burnMemory(ctx context.Context, targetMB, stepMB int, stepInterval time.Duration) {
	if targetMB <= 0 {
		return
	}
	var ballast [][]byte
	heldMB := 0
	ticker := time.NewTicker(stepInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if heldMB >= targetMB {
				continue
			}
			step := stepMB
			if heldMB+step > targetMB {
				step = targetMB - heldMB
			}
			block := make([]byte, step*1024*1024)
			for i := range block {
				block[i] = byte(i)
			}
			ballast = append(ballast, block)
			heldMB += step
			slog.Info("batch memory grown", "held_mb", heldMB, "target_mb", targetMB)
		}
	}
}

func envOrDefault(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func intFromEnv(name string, fallback int) int {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		slog.Warn("invalid int env; using default", "variable", name, "value", value, "default", fallback)
		return fallback
	}
	return parsed
}

func durationFromEnv(name string, fallback time.Duration) time.Duration {
	value := os.Getenv(name)
	if value == "" {
		return fallback
	}
	parsed, err := time.ParseDuration(value)
	if err != nil || parsed <= 0 {
		slog.Warn("invalid duration env; using default", "variable", name, "value", value, "default", fallback)
		return fallback
	}
	return parsed
}
