package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestExpectedAndDeliberateDenials(t *testing.T) {
	f := flow{Action: "Deny", SourceNamespace: "shop", SourceLabels: "app.kubernetes.io/name=api", DestNamespace: "shop", DestLabels: "app.kubernetes.io/name=postgres", Protocol: "tcp", Port: 5432, Reporter: "Src", PacketsOut: 3, End: time.Unix(100, 0)}
	stray := f
	stray.SourceLabels = "app.kubernetes.io/name=matrix-probe"
	allowed := f
	allowed.Action = "Allow"
	allowed.PacketsOut = 100
	otherNS := f
	otherNS.SourceNamespace = "other"
	packets, latest, records, err := summarize([]flow{f, stray, allowed, otherNS}, "shop")
	if err != nil || latest != 100 || records != 2 || len(packets) != 2 {
		t.Fatalf("unexpected summary: %v %d %d %v", packets, latest, records, err)
	}
	var unexpected int64
	for k, n := range packets {
		if strings.Contains(k, `expected_allow="true"`) {
			unexpected += n
		}
	}
	if unexpected != 3 {
		t.Fatal(unexpected)
	}
	f.Reporter = "Dst"
	packets, _, _, err = summarize([]flow{stray, f}, "shop")
	if err != nil || len(packets) != 2 {
		t.Fatal("reporters were combined")
	}
}

func TestFetchRejectsBrokenOrIncompleteData(t *testing.T) {
	for _, body := range []string{`<html>proxy fallback</html>`, `{}`, `{"total":{"totalPages":2},"items":[]}`} {
		s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(body)) }))
		_, err := fetch(s.Client(), s.URL, "shop", time.Now())
		s.Close()
		if err == nil {
			t.Fatalf("accepted %s", body)
		}
	}
}

func TestFailureDoesNotPublishZeroDenials(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) }))
	defer s.Close()
	e := &exporter{client: s.Client(), endpoint: s.URL, namespace: "shop"}
	e.collect()
	if !strings.Contains(e.metrics, "shop_flow_collector_success 0") || strings.Contains(e.metrics, "shop_flow_unexpected_denied_packets_window") {
		t.Fatal(e.metrics)
	}
}

func TestEmptyWindowIsValidZero(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("filters") == "" {
			t.Error("namespace filter missing")
		}
		w.Write([]byte(`{"total":{"totalPages":0},"items":[]}`))
	}))
	defer s.Close()
	e := &exporter{client: s.Client(), endpoint: s.URL, namespace: "shop"}
	e.collect()
	if !strings.Contains(e.metrics, "shop_flow_unexpected_denied_packets_window 0") {
		t.Fatal(e.metrics)
	}
}
