package main

import (
	"bufio"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestReadOnlyStateAndConditionalReassignment(t *testing.T) {
	ctx := context.Background()
	dir := filepath.Join(t.TempDir(), "missing")
	if _, err := openStore(ctx, dir, false); !errors.Is(err, errMissing) {
		t.Fatal(err)
	}
	if _, err := os.Stat(dir); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("read-only command created state", err)
	}
	s, err := openStore(ctx, dir, true)
	if err != nil {
		t.Fatal(err)
	}
	defer s.db.Close()
	p, _ := parseProfile([]string{"fixture", "--host", "alias"})
	if err := s.add(ctx, p); err != nil {
		t.Fatal(err)
	}
	old, err := s.get(ctx, p.Name)
	if err != nil {
		t.Fatal(err)
	}
	if ok, err := s.remove(ctx, old); !ok || err != nil {
		t.Fatal(ok, err)
	}
	replacement, _ := parseProfile([]string{"fixture", "--host", "new-alias"})
	if err := s.add(ctx, replacement); err != nil {
		t.Fatal(err)
	}
	if ok, err := s.change(ctx, old); ok || err != nil {
		t.Fatal("stale update", ok, err)
	}
	if ok, err := s.remove(ctx, old); ok || err != nil {
		t.Fatal("stale remove", ok, err)
	}
}

func TestPrunePreservesLiveStartingAndBlockedRecords(t *testing.T) {
	ctx := context.Background()
	s, err := openStore(ctx, t.TempDir(), true)
	if err != nil {
		t.Fatal(err)
	}
	defer s.db.Close()
	for _, name := range []string{"dead", "live", "starting", "blocked"} {
		p, _ := parseProfile([]string{name, "--host", "alias"})
		p.Desired, p.Owner, p.Port, p.Phase = true, "owner", 40123, name
		p.Deadline = time.Now().Add(-time.Second)
		if name == "starting" {
			p.Deadline = time.Now().Add(startLease)
		}
		if err := s.add(ctx, p); err != nil {
			t.Fatal(err)
		}
		if name == "live" {
			lock := s.lock(p.ID)
			defer lock.Close()
			if ok, err := lock.TryLock(); !ok || err != nil {
				t.Fatal(ok, err)
			}
		}
	}
	reaped, err := prune(ctx, s)
	if err != nil || len(reaped) != 1 || reaped[0] != "dead" {
		t.Fatal(reaped, err)
	}
	dead, err := s.get(ctx, "dead")
	if err != nil || dead.Port != 40123 || dead.Owner != "" || dead.Desired {
		t.Fatal(dead, err)
	}
}

func TestLeaseBoundsClockRollback(t *testing.T) {
	now := time.Now()
	p := profile{Deadline: now.Add(startLease)}
	if !leaseCurrent(p, now) || leaseCurrent(p, now.Add(-time.Hour)) || leaseCurrent(p, now.Add(time.Hour)) {
		t.Fatal("unexpected lease age after clock change")
	}
}

func TestDirectEndpointRespectsOpenSSHRoute(t *testing.T) {
	base := "hostname ssh.example\nport 2222\n"
	if got := directEndpoint(base + "proxycommand none\n"); got != "ssh.example:2222" {
		t.Fatal(got)
	}
	if got := directEndpoint(base + "proxyjump bastion\n"); got != "" {
		t.Fatal("attempted to bypass jump host", got)
	}
	if got := directEndpoint(base + "proxycommand ssh gateway -W %h:%p\n"); got != "" {
		t.Fatal("attempted to bypass proxy command", got)
	}
}

func TestStopRetainsUnverifiedCleanup(t *testing.T) {
	ctx := context.Background()
	s, err := openStore(ctx, t.TempDir(), true)
	if err != nil {
		t.Fatal(err)
	}
	defer s.db.Close()
	p, _ := parseProfile([]string{"fixture", "--host", "alias"})
	p.Phase, p.Owner, p.Desired, p.CleanupFailed = "blocked", "owner", true, true
	p.Problem = "unverified descendant"
	if err := s.add(ctx, p); err != nil {
		t.Fatal(err)
	}
	if _, err := stop(ctx, s, p.Name); err == nil {
		t.Fatal("stop reported success")
	}
	check, err := s.get(ctx, p.Name)
	if err != nil || check.Phase != "blocked" || check.Owner != "owner" {
		t.Fatal(check, err)
	}
}

func TestStopInvalidatesInFlightStart(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	s, err := openStore(ctx, t.TempDir(), true)
	if err != nil {
		t.Fatal(err)
	}
	defer s.db.Close()
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		entered <- struct{}{}
		<-release
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()
	p, _ := parseProfile([]string{"fixture", "--host", "alias"})
	p.Port, _ = strconv.Atoi(server.URL[strings.LastIndexByte(server.URL, ':')+1:])
	p.Owner, p.Desired, p.Phase = "reserved", true, "starting"
	p.Deadline = time.Now().Add(startLease)
	if err := s.add(ctx, p); err != nil {
		t.Fatal(err)
	}
	finished := make(chan error, 1)
	go func() {
		_, err := start(ctx, s, p.Name, false)
		finished <- err
	}()
	select {
	case <-entered:
	case <-ctx.Done():
		close(release)
		t.Fatal("start did not read the reserved profile")
	}
	_, err = stop(ctx, s, p.Name)
	close(release)
	if err != nil {
		t.Fatal(err)
	}
	if err := <-finished; err == nil || !strings.Contains(err.Error(), "changed or is stopping") {
		t.Fatal("stale start did not yield to stop", err)
	}
}

func TestHealthRejectsForeignListener(t *testing.T) {
	owner, _ := token()
	foreign, _ := token()
	server := httptest.NewServer(newRelay(12345, foreign))
	defer server.Close()
	port, _ := strconv.Atoi(server.URL[strings.LastIndexByte(server.URL, ':')+1:])
	if _, err := localHealth(context.Background(), profile{Port: port, Owner: owner}); err == nil {
		t.Fatal("accepted a foreign gateway")
	}
	if h, err := localHealth(context.Background(), profile{Port: port, Owner: foreign}); err != nil || h.Phase != "starting" {
		t.Fatal(h, err)
	}
}

func TestSavedPortConflictPreservesPortAndForeignListener(t *testing.T) {
	ctx := context.Background()
	s, err := openStore(ctx, t.TempDir(), true)
	if err != nil {
		t.Fatal(err)
	}
	defer s.db.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	p, _ := parseProfile([]string{"fixture", "--host", "alias"})
	p.Port, _ = strconv.Atoi(server.URL[strings.LastIndexByte(server.URL, ':')+1:])
	p.Owner, _ = token()
	p.Desired, p.Phase = true, "starting"
	if err := s.add(ctx, p); err != nil {
		t.Fatal(err)
	}
	if err := serve(s, p.Name, p.Owner); err == nil {
		t.Fatal("gateway silently chose a different public port")
	}
	blocked, err := s.get(ctx, p.Name)
	if err != nil || blocked.Port != p.Port || blocked.Phase != "blocked" {
		t.Fatal(blocked, err)
	}
	resp, err := healthClient.Get(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNoContent {
		t.Fatal("foreign listener changed", resp.StatusCode)
	}
}

func listen(t *testing.T, handler func(net.Conn)) string {
	t.Helper()
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func() { defer conn.Close(); handler(conn) }()
		}
	}()
	return ln.Addr().String()
}

func TestSOCKSSendsRemoteHostname(t *testing.T) {
	request := make(chan string, 1)
	addr := listen(t, func(conn net.Conn) {
		var greeting [3]byte
		io.ReadFull(conn, greeting[:])
		// Fragment the reply to exercise the full-read path.
		conn.Write([]byte{5})
		conn.Write([]byte{0})
		var header [5]byte
		io.ReadFull(conn, header[:])
		if header[0] != 5 || header[1] != 1 || header[3] != 3 {
			return
		}
		b := make([]byte, int(header[4])+2)
		io.ReadFull(conn, b)
		request <- string(b[:len(b)-2])
		conn.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 1})
	})
	conn, err := socksOpen(context.Background(), addr, "remote-dns.invalid:80")
	if err != nil {
		t.Fatal(err)
	}
	conn.Close()
	if got := <-request; got != "remote-dns.invalid" {
		t.Fatal(got)
	}
}

func TestSOCKSRejectsMalformedRepliesAndCancels(t *testing.T) {
	for _, greeting := range [][]byte{{4, 0}, {5, 2}, {5}} {
		addr := listen(t, func(conn net.Conn) {
			io.CopyN(io.Discard, conn, 3)
			conn.Write(greeting)
		})
		if _, err := socksOpen(context.Background(), addr, ""); err == nil {
			t.Fatal(greeting)
		}
	}
	addr := listen(t, func(conn net.Conn) { io.Copy(io.Discard, conn) })
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Millisecond)
	defer cancel()
	start := time.Now()
	if conn, err := socksOpen(ctx, addr, ""); err == nil || conn != nil {
		t.Fatal(conn, err)
	}
	if time.Since(start) > time.Second {
		t.Fatal("SOCKS cancellation was delayed")
	}
}

func TestLateDialCannotJoinNewGeneration(t *testing.T) {
	reached := make(chan struct{})
	release := make(chan struct{})
	addr := listen(t, func(conn net.Conn) {
		io.CopyN(io.Discard, conn, 3)
		conn.Write([]byte{5, 0})
		var header [5]byte
		io.ReadFull(conn, header[:])
		io.CopyN(io.Discard, conn, int64(header[4])+2)
		close(reached)
		<-release
		conn.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 1})
	})
	defer close(release)
	owner, _ := token()
	r := newRelay(12345, owner)
	defer r.close()
	r.publish("ready", addr, 1)
	done := make(chan error, 1)
	go func() {
		conn, err := r.dial(context.Background(), "tcp", "remote.invalid:80")
		if conn != nil {
			conn.Close()
		}
		done <- err
	}()
	select {
	case <-reached:
	case <-time.After(time.Second):
		t.Fatal("dial did not reach SOCKS")
	}
	r.publish("recovering", "", 1)
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("late dial joined inactive generation")
		}
	case <-time.After(time.Second):
		t.Fatal("retired generation did not cancel dial")
	}
}

func TestRelayRejectsLoopAndHopBudget(t *testing.T) {
	owner, _ := token()
	r := newRelay(12345, owner)
	defer r.close()
	for _, url := range []string{"http://localhost.:12345/", "http://127.0.0.2:12345/", "http://[::ffff:127.0.0.1]:12345/"} {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, url, nil))
		if w.Code != http.StatusLoopDetected {
			t.Fatalf("%s -> %d", url, w.Code)
		}
	}
	w := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "http://unresolved.invalid/", nil)
	for i := 0; i < 13; i++ {
		req.Header.Add("Via", "1.1 another-proxy")
	}
	r.ServeHTTP(w, req)
	if w.Code != http.StatusLoopDetected {
		t.Fatal(w.Code)
	}
}

func TestConnectPreservesBufferedBytesAndDrainsOldStream(t *testing.T) {
	closed := make(chan struct{})
	addr := listen(t, func(conn net.Conn) {
		defer close(closed)
		io.CopyN(io.Discard, conn, 3)
		conn.Write([]byte{5, 0})
		var header [5]byte
		io.ReadFull(conn, header[:])
		io.CopyN(io.Discard, conn, int64(header[4])+2)
		conn.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 1})
		io.Copy(conn, conn)
	})
	owner, _ := token()
	r := newRelay(12345, owner)
	defer r.close()
	r.publish("ready", addr, 1)
	server := httptest.NewServer(r)
	defer server.Close()
	conn, err := net.Dial("tcp", strings.TrimPrefix(server.URL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(drainTimeout + 2*time.Second))
	_, err = io.WriteString(conn, "CONNECT example.invalid:443 HTTP/1.1\r\nHost: example.invalid:443\r\n\r\npipelined")
	if err != nil {
		t.Fatal(err)
	}
	buffer := bufio.NewReader(conn)
	response, err := http.ReadResponse(buffer, &http.Request{Method: http.MethodConnect})
	if err != nil || response.StatusCode != http.StatusOK {
		t.Fatal(response, err)
	}
	pipelined := make([]byte, len("pipelined"))
	if _, err := io.ReadFull(buffer, pipelined); err != nil || string(pipelined) != "pipelined" {
		t.Fatal(string(pipelined), err)
	}
	r.publish("recovering", "", 1)
	if _, err := buffer.ReadByte(); err == nil || os.IsTimeout(err) {
		t.Fatal("retired client remained connected", err)
	}
	select {
	case <-closed:
	case <-time.After(time.Second):
		t.Fatal("retired upstream remained connected")
	}
}
