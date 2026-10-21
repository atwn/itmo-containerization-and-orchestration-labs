package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"time"
)

const serviceAccountPath = "/var/run/secrets/kubernetes.io/serviceaccount"

type prometheusResponse struct {
	Status string `json:"status"`
	Data   struct {
		Result []struct {
			Value []any `json:"value"`
		} `json:"result"`
	} `json:"data"`
}

type gate struct {
	client                                           *http.Client
	prometheusURL, patchURL, token                   string
	errorQuery, latencyQuery, requestQuery           string
	errorThreshold, latencyThreshold, minRequestRate float64
	step, successWindows                             int
	interval, warmup                                 time.Duration
	weight                                           int
}

func main() {
	g, err := newGate()
	if err != nil {
		log.Fatal(err)
	}
	g.run(context.Background())
}

func newGate() (*gate, error) {
	token, err := os.ReadFile(serviceAccountPath + "/token")
	if err != nil {
		return nil, fmt.Errorf("read service account token: %w", err)
	}
	ca, err := os.ReadFile(serviceAccountPath + "/ca.crt")
	if err != nil {
		return nil, fmt.Errorf("read service account CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		return nil, fmt.Errorf("parse service account CA")
	}
	host, port := env("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"), env("KUBERNETES_SERVICE_PORT_HTTPS", "443")
	namespace, routeName := env("POD_NAMESPACE", "shop"), env("ROUTE_NAME", "shop-api")
	return &gate{
		client:         &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}}},
		prometheusURL:  env("PROMETHEUS_URL", "http://kube-prometheus-stack-prometheus.monitoring:9090"),
		patchURL:       fmt.Sprintf("https://%s:%s/apis/policy.linkerd.io/v1beta2/namespaces/%s/httproutes/%s", host, port, url.PathEscape(namespace), url.PathEscape(routeName)),
		token:          string(token),
		errorQuery:     env("ERROR_QUERY", `(sum(rate(shop_api_http_requests_total{route="/order",version="v2",status=~"5.."}[1m])) or on() vector(0)) / clamp_min((sum(rate(shop_api_http_requests_total{route="/order",version="v2"}[1m])) or on() vector(0)), 0.001)`),
		latencyQuery:   env("LATENCY_QUERY", `histogram_quantile(0.95, sum by (le) (rate(shop_api_http_request_duration_seconds_bucket{route="/order",version="v2"}[1m])))`),
		requestQuery:   env("REQUEST_RATE_QUERY", `sum(rate(shop_api_http_requests_total{route="/order",version="v2"}[1m]))`),
		errorThreshold: envFloat("ERROR_THRESHOLD", 0.05), latencyThreshold: envFloat("LATENCY_THRESHOLD_SECONDS", 0.5), minRequestRate: envFloat("MIN_REQUEST_RATE", 0.1),
		step: envInt("STEP_PERCENT", 10), successWindows: envInt("SUCCESS_WINDOWS", 2), interval: time.Duration(envInt("INTERVAL_SECONDS", 15)) * time.Second,
		warmup: time.Duration(envInt("WARMUP_SECONDS", 60)) * time.Second, weight: envInt("INITIAL_V2_WEIGHT", 10),
	}, nil
}

func (g *gate) run(ctx context.Context) {
	successes := 0
	if err := g.setWeight(ctx, g.weight); err != nil {
		log.Printf("initial weight failed: %v", err)
	}
	log.Printf("warmup: v2=%d%% for %s", g.weight, g.warmup)
	select {
	case <-ctx.Done():
		return
	case <-time.After(g.warmup):
	}
	for {
		requestRate, requestErr := g.query(ctx, g.requestQuery)
		errorRate, errorErr := g.query(ctx, g.errorQuery)
		p95, latencyErr := g.query(ctx, g.latencyQuery)
		if requestErr != nil || errorErr != nil || latencyErr != nil {
			successes = 0
			log.Printf("hold: metrics unavailable request=%v error=%v latency=%v", requestErr, errorErr, latencyErr)
		} else if requestRate < g.minRequestRate {
			successes = 0
			log.Printf("hold: insufficient traffic weight=%d request_rate=%.3f/s", g.weight, requestRate)
		} else if errorRate > g.errorThreshold || p95 > g.latencyThreshold {
			successes = 0
			if g.weight != 0 {
				if err := g.setWeight(ctx, 0); err != nil {
					log.Printf("rollback failed: %v", err)
				} else {
					g.weight = 0
					log.Printf("rollback: v2=0%% error_rate=%.3f p95=%.3fs", errorRate, p95)
				}
			}
		} else if g.weight < 100 {
			successes++
			log.Printf("healthy window %d/%d: weight=%d error_rate=%.3f p95=%.3fs", successes, g.successWindows, g.weight, errorRate, p95)
			if successes >= g.successWindows {
				next := min(100, g.weight+g.step)
				if err := g.setWeight(ctx, next); err != nil {
					log.Printf("promotion failed: %v", err)
				} else {
					g.weight = next
					successes = 0
					log.Printf("promote: v2=%d%%", g.weight)
				}
			}
		} else {
			log.Printf("promotion complete: v2=100%%")
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(g.interval):
		}
	}
}

func (g *gate) query(ctx context.Context, expression string) (float64, error) {
	endpoint := g.prometheusURL + "/api/v1/query?" + url.Values{"query": {expression}}.Encode()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	resp, err := g.client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	var payload prometheusResponse
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return 0, err
	}
	if payload.Status != "success" || len(payload.Data.Result) != 1 || len(payload.Data.Result[0].Value) != 2 {
		return 0, fmt.Errorf("unexpected Prometheus result")
	}
	text, ok := payload.Data.Result[0].Value[1].(string)
	if !ok {
		return 0, fmt.Errorf("Prometheus value is not a string")
	}
	value, err := strconv.ParseFloat(text, 64)
	if err != nil || math.IsNaN(value) || math.IsInf(value, 0) {
		return 0, fmt.Errorf("non-finite Prometheus value %q", text)
	}
	return value, nil
}

func (g *gate) setWeight(ctx context.Context, v2 int) error {
	patch := []map[string]any{{"op": "replace", "path": "/spec/rules/0/backendRefs/0/weight", "value": 100 - v2}, {"op": "replace", "path": "/spec/rules/0/backendRefs/1/weight", "value": v2}}
	body, _ := json.Marshal(patch)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPatch, g.patchURL, bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+g.token)
	req.Header.Set("Content-Type", "application/json-patch+json")
	resp, err := g.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		message, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return fmt.Errorf("Kubernetes API %s: %s", resp.Status, message)
	}
	return nil
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
func envFloat(name string, fallback float64) float64 {
	value, err := strconv.ParseFloat(os.Getenv(name), 64)
	if err != nil {
		return fallback
	}
	return value
}
func envInt(name string, fallback int) int {
	value, err := strconv.Atoi(os.Getenv(name))
	if err != nil {
		return fallback
	}
	return value
}
