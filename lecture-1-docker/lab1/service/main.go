package main

import (
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"sync"
)

const mib = 1024 * 1024

type service struct {
	mu          sync.Mutex
	allocations [][]byte
	burnOnce    sync.Once
	stop        chan struct{}
	done        chan struct{}
}

func newService() *service {
	return &service{stop: make(chan struct{}), done: make(chan struct{})}
}

func (s *service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/health", "/eat", "/burn":
	default:
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	switch r.URL.Path {
	case "/health":
		fmt.Fprint(w, "ok")
	case "/eat":
		n, err := strconv.ParseInt(r.URL.Query().Get("mb"), 10, 64)
		maxInt := int64(int(^uint(0) >> 1))
		if err != nil || n <= 0 || n > maxInt/mib {
			http.Error(w, "mb must be a positive integer whose byte count fits in int", http.StatusBadRequest)
			return
		}
		memory := make([]byte, int(n)*mib)
		// Touch each OS page: reserving virtual memory alone does not commit RAM.
		for i := 0; i < len(memory); i += os.Getpagesize() {
			memory[i] = 1
		}
		memory[len(memory)-1] = 1
		s.mu.Lock()
		s.allocations = append(s.allocations, memory)
		s.mu.Unlock()
		fmt.Fprintf(w, "retained %d MiB\n", n)
	case "/burn":
		s.startBurn()
		fmt.Fprintln(w, "CPU worker running")
	}
}

func (s *service) startBurn() {
	s.burnOnce.Do(func() {
		go func() {
			defer close(s.done)
			// One runnable goroutine consumes roughly one core. The stop channel
			// allows tests to clean up; production keeps running until exit.
			for {
				select {
				case <-s.stop:
					return
				default:
				}
			}
		}()
	})
}

func main() {
	log.Print("api listening on :8080")
	log.Fatal(http.ListenAndServe(":8080", newService()))
}
