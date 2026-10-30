// Export bounded-window observations from the Whisker v3.30 HTTP API.
// These gauges describe aggregated packet observations, not unique connections.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type flow struct {
	End             time.Time `json:"end_time"`
	Action          string    `json:"action"`
	SourceNamespace string    `json:"source_namespace"`
	SourceLabels    string    `json:"source_labels"`
	DestNamespace   string    `json:"dest_namespace"`
	DestLabels      string    `json:"dest_labels"`
	Protocol        string    `json:"protocol"`
	Port            int       `json:"dest_port"`
	Reporter        string    `json:"reporter"`
	PacketsIn       int64     `json:"packets_in"`
	PacketsOut      int64     `json:"packets_out"`
}

type response struct {
	Total struct {
		Pages int `json:"totalPages"`
	} `json:"total"`
	Items *[]flow `json:"items"`
}

func label(labels, key string) string {
	for _, part := range strings.Split(labels, "|") {
		kv := strings.SplitN(strings.TrimSpace(part), "=", 2)
		if len(kv) == 2 && kv[0] == key {
			return kv[1]
		}
	}
	return ""
}

func role(labels string) string {
	name := label(labels, "app.kubernetes.io/name")
	switch name {
	case "api", "worker", "postgres", "chaos-load":
		return name
	}
	if label(labels, "k8s-app") == "kube-dns" {
		return "dns"
	}
	return "other"
}

func summarize(flows []flow, namespace string) (map[string]int64, int64, int64, error) {
	packets := map[string]int64{}
	var latest, records int64
	for _, f := range flows {
		if f.SourceNamespace != namespace {
			continue
		}
		if f.End.Unix() > latest {
			latest = f.End.Unix()
		}
		if f.Action != "Deny" {
			continue
		}
		if f.PacketsIn < 0 || f.PacketsOut < 0 {
			return nil, 0, 0, fmt.Errorf("negative packet count")
		}
		// Preserve reporter: Src and Dst must not be mistaken for independent connections.
		reporter := f.Reporter
		if reporter != "Src" && reporter != "Dst" {
			return nil, 0, 0, fmt.Errorf("unknown reporter")
		}
		protocol := f.Protocol
		if protocol != "tcp" && protocol != "udp" {
			protocol = "other"
		}
		port := "other"
		switch f.Port {
		case 53, 5432, 8080, 443:
			port = strconv.Itoa(f.Port)
		}
		src, dst := role(f.SourceLabels), role(f.DestLabels)
		// An expected application route, used later to distinguish regressions
		// from deliberate stray-Pod denials. Do not infer Deny from a timeout.
		expected := "false"
		if src == "api" || src == "worker" {
			if (dst == "postgres" && f.DestNamespace == namespace && protocol == "tcp" && f.Port == 5432) ||
				(dst == "dns" && f.DestNamespace == "kube-system" && (protocol == "tcp" || protocol == "udp") && f.Port == 53) {
				expected = "true"
			}
		}
		key := fmt.Sprintf("source=%q,destination=%q,protocol=%q,port=%q,reporter=%q,expected_allow=%q", src, dst, protocol, port, reporter, expected)
		packets[key] += f.PacketsIn + f.PacketsOut
		records++
	}
	return packets, latest, records, nil
}

func fetch(client *http.Client, endpoint, namespace string, now time.Time) ([]flow, error) {
	u, err := url.Parse(endpoint)
	if err != nil {
		return nil, err
	}
	filters, _ := json.Marshal(map[string]any{"source_namespaces": []map[string]string{{"value": namespace, "type": "Exact"}}})
	q := u.Query()
	q.Set("startTimeGte", strconv.FormatInt(now.Unix()-120, 10))
	q.Set("startTimeLt", strconv.FormatInt(now.Unix(), 10))
	q.Set("filters", string(filters))
	u.RawQuery = q.Encode()
	r, err := client.Get(u.String())
	if err != nil {
		return nil, err
	}
	defer r.Body.Close()
	if r.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Whisker HTTP %d", r.StatusCode)
	}
	var body response
	if err = json.NewDecoder(io.LimitReader(r.Body, 8<<20)).Decode(&body); err != nil {
		return nil, err
	}
	if body.Items == nil || body.Total.Pages > 1 {
		return nil, fmt.Errorf("missing items or incomplete page set")
	}
	return *body.Items, nil
}

type exporter struct {
	mu                  sync.RWMutex
	metrics             string
	client              *http.Client
	endpoint, namespace string
}

func (e *exporter) collect() {
	now := time.Now()
	flows, err := fetch(e.client, e.endpoint, e.namespace, now)
	var packets map[string]int64
	var latest, records int64
	if err == nil {
		packets, latest, records, err = summarize(flows, e.namespace)
	}
	var b strings.Builder
	b.WriteString("# HELP shop_flow_collector_success Last Whisker query and schema validation succeeded.\n# TYPE shop_flow_collector_success gauge\n")
	if err != nil {
		log.Printf("Whisker collection failed: %v", err)
		b.WriteString("shop_flow_collector_success 0\n")
	} else {
		b.WriteString("shop_flow_collector_success 1\n")
		fmt.Fprintf(&b, "# TYPE shop_flow_last_observed_timestamp_seconds gauge\nshop_flow_last_observed_timestamp_seconds %d\n", latest)
		fmt.Fprintf(&b, "# TYPE shop_flow_window_seconds gauge\nshop_flow_window_seconds 120\n# TYPE shop_flow_denied_records_window gauge\nshop_flow_denied_records_window %d\n", records)
		b.WriteString("# HELP shop_flow_denied_packets_window Denied packet observations in the last 120 seconds; overlapping window gauge, never use rate().\n# TYPE shop_flow_denied_packets_window gauge\n")
		keys := make([]string, 0, len(packets))
		for k := range packets {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			fmt.Fprintf(&b, "shop_flow_denied_packets_window{%s} %d\n", k, packets[k])
		}
		var unexpected int64
		for k, n := range packets {
			if strings.Contains(k, `expected_allow="true"`) {
				unexpected += n
			}
		}
		fmt.Fprintf(&b, "# TYPE shop_flow_unexpected_denied_packets_window gauge\nshop_flow_unexpected_denied_packets_window %d\n", unexpected)
	}
	e.mu.Lock()
	e.metrics = b.String()
	e.mu.Unlock()
}

func main() {
	endpoint := os.Getenv("WHISKER_URL")
	if endpoint == "" {
		log.Fatal("WHISKER_URL is required")
	}
	namespace := os.Getenv("SOURCE_NAMESPACE")
	if namespace == "" {
		namespace = "shop"
	}
	e := &exporter{client: &http.Client{Timeout: 8 * time.Second}, endpoint: endpoint, namespace: namespace}
	e.collect()
	go func() {
		for range time.NewTicker(15 * time.Second).C {
			e.collect()
		}
	}()
	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) { fmt.Fprintln(w, "ok") })
	http.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		e.mu.RLock()
		defer e.mu.RUnlock()
		io.WriteString(w, e.metrics)
	})
	log.Fatal((&http.Server{Addr: ":9095", ReadHeaderTimeout: 5 * time.Second}).ListenAndServe())
}
