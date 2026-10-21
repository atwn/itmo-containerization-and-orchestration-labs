package main

import (
	"bytes"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

var bucketBounds = []float64{0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5}

type metrics struct {
	mu       sync.Mutex
	requests map[string]uint64
	versions map[string]uint64
	buckets  []uint64
	count    uint64
	sum      float64
}

var observed = &metrics{requests: map[string]uint64{}, versions: map[string]uint64{}, buckets: make([]uint64, len(bucketBounds))}

func main() {
	baseURL := env("TARGET_URL", "http://shop-api")
	interval := envDuration("INTERVAL", 100*time.Millisecond)
	client := &http.Client{Timeout: 3 * time.Second}

	go func() {
		for {
			request(client, http.MethodPost, baseURL+"/order", []byte(`{"sku":"mesh-lab","quantity":1}`), "/order")
			time.Sleep(interval)
		}
	}()
	go func() {
		for {
			version(client, baseURL+"/version")
			time.Sleep(interval)
		}
	}()

	http.HandleFunc("/metrics", observed.serve)
	http.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("ok")) })
	log.Printf("loadgen targeting %s every %s", baseURL, interval)
	log.Fatal(http.ListenAndServe(":8080", nil))
}

func request(client *http.Client, method, url string, body []byte, route string) {
	started := time.Now()
	req, err := http.NewRequest(method, url, bytes.NewReader(body))
	if err == nil {
		req.Header.Set("Content-Type", "application/json")
	}
	status := "transport_error"
	if err == nil {
		resp, doErr := client.Do(req)
		if doErr == nil {
			status = strconv.Itoa(resp.StatusCode)
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
		}
	}
	observed.observe(method, route, status, time.Since(started).Seconds())
}

func version(client *http.Client, url string) {
	resp, err := client.Get(url)
	if err != nil {
		observed.addVersion("transport_error")
		return
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 32))
	_ = resp.Body.Close()
	value := strings.TrimSpace(string(body))
	if value != "v1" && value != "v2" {
		value = "unknown"
	}
	observed.addVersion(value)
}

func (m *metrics) observe(method, route, status string, seconds float64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.requests[method+"\x00"+route+"\x00"+status]++
	m.count++
	m.sum += seconds
	for index, bound := range bucketBounds {
		if seconds <= bound {
			m.buckets[index]++
		}
	}
}

func (m *metrics) addVersion(version string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.versions[version]++
}

func (m *metrics) serve(w http.ResponseWriter, _ *http.Request) {
	m.mu.Lock()
	defer m.mu.Unlock()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	_, _ = fmt.Fprintln(w, "# HELP shop_loadgen_requests_total Requests observed by the load generator after mesh processing.")
	_, _ = fmt.Fprintln(w, "# TYPE shop_loadgen_requests_total counter")
	requestKeys := make([]string, 0, len(m.requests))
	for key := range m.requests {
		requestKeys = append(requestKeys, key)
	}
	sort.Strings(requestKeys)
	for _, key := range requestKeys {
		parts := strings.Split(key, "\x00")
		_, _ = fmt.Fprintf(w, "shop_loadgen_requests_total{method=%q,route=%q,status=%q} %d\n", parts[0], parts[1], parts[2], m.requests[key])
	}
	_, _ = fmt.Fprintln(w, "# HELP shop_loadgen_request_duration_seconds End-to-end request latency observed by the load generator.")
	_, _ = fmt.Fprintln(w, "# TYPE shop_loadgen_request_duration_seconds histogram")
	for index, bound := range bucketBounds {
		_, _ = fmt.Fprintf(w, "shop_loadgen_request_duration_seconds_bucket{method=\"POST\",route=\"/order\",le=%q} %d\n", strconv.FormatFloat(bound, 'g', -1, 64), m.buckets[index])
	}
	_, _ = fmt.Fprintf(w, "shop_loadgen_request_duration_seconds_bucket{method=\"POST\",route=\"/order\",le=\"+Inf\"} %d\n", m.count)
	_, _ = fmt.Fprintf(w, "shop_loadgen_request_duration_seconds_sum{method=\"POST\",route=\"/order\"} %g\n", m.sum)
	_, _ = fmt.Fprintf(w, "shop_loadgen_request_duration_seconds_count{method=\"POST\",route=\"/order\"} %d\n", m.count)
	_, _ = fmt.Fprintln(w, "# HELP shop_loadgen_version_responses_total Version endpoint responses used to measure the traffic split.")
	_, _ = fmt.Fprintln(w, "# TYPE shop_loadgen_version_responses_total counter")
	versionKeys := make([]string, 0, len(m.versions))
	for key := range m.versions {
		versionKeys = append(versionKeys, key)
	}
	sort.Strings(versionKeys)
	for _, key := range versionKeys {
		_, _ = fmt.Fprintf(w, "shop_loadgen_version_responses_total{version=%q} %d\n", key, m.versions[key])
	}
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func envDuration(name string, fallback time.Duration) time.Duration {
	value, err := time.ParseDuration(os.Getenv(name))
	if err != nil {
		return fallback
	}
	return value
}
