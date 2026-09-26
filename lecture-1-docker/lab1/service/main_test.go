package main

import (
	"net/http/httptest"
	"os"
	"sync"
	"testing"
)

func TestRoutes(t *testing.T) {
	s := newService()
	for _, tc := range []struct {
		method string
		path   string
		status int
	}{
		{"GET", "/health", 200},
		{"POST", "/health", 405},
		{"POST", "/eat?mb=1", 405},
		{"POST", "/burn", 405},
		{"GET", "/missing", 404},
	} {
		w := httptest.NewRecorder()
		s.ServeHTTP(w, httptest.NewRequest(tc.method, tc.path, nil))
		if w.Code != tc.status {
			t.Fatalf("%s %s: status %d, want %d", tc.method, tc.path, w.Code, tc.status)
		}
		if tc.status == 405 && w.Header().Get("Allow") != "GET" {
			t.Fatal("missing Allow: GET")
		}
		if tc.status == 200 && w.Body.String() != "ok" {
			t.Fatalf("health body = %q", w.Body.String())
		}
	}
}

func TestInvalidAllocations(t *testing.T) {
	s := newService()
	for _, value := range []string{"", "0", "-1", "abc", "1.5", "9223372036854775807", "9223372036854775808"} {
		w := httptest.NewRecorder()
		s.ServeHTTP(w, httptest.NewRequest("GET", "/eat?mb="+value, nil))
		if w.Code != 400 {
			t.Errorf("mb=%q: status %d, want 400", value, w.Code)
		}
	}
	if len(s.allocations) != 0 {
		t.Fatal("invalid requests retained memory")
	}
}

func TestConcurrentAllocations(t *testing.T) {
	s := newService()
	var requests sync.WaitGroup
	for i := 0; i < 4; i++ {
		requests.Add(1)
		go func() {
			defer requests.Done()
			w := httptest.NewRecorder()
			s.ServeHTTP(w, httptest.NewRequest("GET", "/eat?mb=1", nil))
			if w.Code != 200 {
				t.Errorf("allocation status = %d", w.Code)
			}
		}()
	}
	requests.Wait()
	if len(s.allocations) != 4 {
		t.Fatalf("retained %d allocations, want 4", len(s.allocations))
	}
	for _, memory := range s.allocations {
		if len(memory) != mib {
			t.Fatalf("allocation length = %d", len(memory))
		}
		for i := 0; i < len(memory); i += os.Getpagesize() {
			if memory[i] != 1 {
				t.Fatalf("page at byte %d was not touched", i)
			}
		}
	}
}

func TestBurnMultipleWorkersAndHealth(t *testing.T) {
	s := newService()
	t.Cleanup(func() {
		s.stopWorkers()
	})
	var requests sync.WaitGroup
	for i := 0; i < 4; i++ {
		requests.Add(1)
		go func() {
			defer requests.Done()
			w := httptest.NewRecorder()
			s.ServeHTTP(w, httptest.NewRequest("GET", "/burn", nil))
			if w.Code != 200 {
				t.Errorf("burn status = %d", w.Code)
			}
		}()
	}
	requests.Wait()
	if got := s.workerCount.Load(); got != 4 {
		t.Fatalf("active CPU workers = %d, want 8", got)
	}
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest("GET", "/health", nil))
	if w.Code != 200 || w.Body.String() != "ok" {
		t.Fatal("health failed while CPU worker was running")
	}
}
