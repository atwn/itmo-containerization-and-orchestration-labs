package main

import (
	"context"
	"encoding/binary"
	"io"
	"net"
	"syscall"
	"testing"
)

func TestTCPConnection(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil { t.Fatal(err) }
	defer listener.Close()
	host, port, _ := net.SplitHostPort(listener.Addr().String())
	if got := probe([]string{"tcp", host, port}); got.Status != "ALLOW" { t.Fatal(got) }
	listener.Close()
	if got := probe([]string{"tcp", host, port}); got.Status != "ERROR" { t.Fatal("refused must not be a policy denial:", got) }
}

func TestFailureClassification(t *testing.T) {
	if got := classify(context.DeadlineExceeded); got.Status != "TIMEOUT" { t.Fatal(got) }
	if got := classify(syscall.ECONNREFUSED); got.Status != "ERROR" { t.Fatal(got) }
	if got := classify(&net.DNSError{Err: "no such host", IsNotFound: true}); got.Status != "ERROR" { t.Fatal(got) }
	if got := probe([]string{"invalid"}); got.Status != "ERROR" { t.Fatal(got) }
}

// Minimal DNS responder for deterministic UDP/TCP tests, without external DNS.
func dnsReply(query []byte) []byte {
	end := 12
	for query[end] != 0 { end += int(query[end]) + 1 }
	end += 5 // root label, QTYPE, QCLASS
	reply := append([]byte(nil), query[:end]...)
	binary.BigEndian.PutUint16(reply[2:4], 0x8180)
	binary.BigEndian.PutUint16(reply[6:8], 0)
	binary.BigEndian.PutUint16(reply[8:10], 0)
	binary.BigEndian.PutUint16(reply[10:12], 0)
	if binary.BigEndian.Uint16(query[end-4:end-2]) == 1 {
		binary.BigEndian.PutUint16(reply[6:8], 1)
		reply = append(reply, 0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 60, 0, 4, 10, 96, 0, 42)
	}
	return reply
}

func TestDNSProtocolsAndAnswer(t *testing.T) {
	udp, err := net.ListenPacket("udp", "127.0.0.1:53")
	if err != nil { t.Fatal(err) }
	defer udp.Close()
	go func() {
		buf := make([]byte, 4096)
		for {
			n, addr, err := udp.ReadFrom(buf)
			if err != nil { return }
			udp.WriteTo(dnsReply(buf[:n]), addr)
		}
	}()
	tcp, err := net.Listen("tcp", "127.0.0.1:53")
	if err != nil { t.Fatal(err) }
	defer tcp.Close()
	go func() {
		for {
			conn, err := tcp.Accept()
			if err != nil { return }
			go func() {
				defer conn.Close()
				header := make([]byte, 2)
				if _, err := io.ReadFull(conn, header); err != nil { return }
				query := make([]byte, binary.BigEndian.Uint16(header))
				if _, err := io.ReadFull(conn, query); err != nil { return }
				reply := dnsReply(query)
				binary.BigEndian.PutUint16(header, uint16(len(reply)))
				conn.Write(append(header, reply...))
			}()
		}
	}()
	for _, protocol := range []string{"udp", "tcp"} {
		if got := probe([]string{"dns", "127.0.0.1", protocol, "shop.test.", "10.96.0.42"}); got.Status != "ALLOW" { t.Fatal(protocol, got) }
		if got := probe([]string{"dns", "127.0.0.1", protocol, "shop.test.", "10.96.0.43"}); got.Status != "ERROR" { t.Fatal(protocol, got) }
	}
}
