// netprobe opens fresh connections without application connection pools.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"time"
)

type result struct {
	Status string `json:"status"`
	Detail string `json:"detail"`
}

func classify(err error) result {
	if err == nil {
		return result{"ALLOW", "connected"}
	}
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return result{"TIMEOUT", err.Error()}
	}
	// Refused, DNS failures and other errors do not prove a policy drop.
	return result{"ERROR", err.Error()}
}

func probe(args []string) result {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	dialer := net.Dialer{}
	if len(args) == 3 && args[0] == "tcp" {
		conn, err := dialer.DialContext(ctx, "tcp", net.JoinHostPort(args[1], args[2]))
		if conn != nil {
			conn.Close()
		}
		return classify(err)
	}
	if len(args) == 5 && args[0] == "dns" && (args[2] == "udp" || args[2] == "tcp") {
		resolver := net.Resolver{PreferGo: true, Dial: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return dialer.DialContext(ctx, args[2], net.JoinHostPort(args[1], "53"))
		}}
		ips, err := resolver.LookupHost(ctx, args[3])
		if err != nil {
			return classify(err)
		}
		for _, ip := range ips {
			if ip == args[4] {
				return result{"ALLOW", fmt.Sprintf("DNS %s: %s", args[2], ip)}
			}
		}
		return result{"ERROR", fmt.Sprintf("unexpected DNS answer: %v", ips)}
	}
	return result{"ERROR", "usage: tcp HOST PORT | dns SERVER udp|tcp NAME EXPECTED_IP"}
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "hold" {
		// No listening socket; the helper cannot become a Service backend.
		for deadline := time.Now().Add(20 * time.Minute); time.Now().Before(deadline); {
			if _, err := os.Stat("/tmp/netprobe-stop"); err == nil {
				return
			}
			time.Sleep(time.Second)
		}
		return
	}
	if len(os.Args) == 2 && os.Args[1] == "stop" {
		if err := os.WriteFile("/tmp/netprobe-stop", nil, 0600); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	// A successful exec always returns JSON; Kubernetes exec failures stay distinct.
	json.NewEncoder(os.Stdout).Encode(probe(os.Args[1:]))
}
