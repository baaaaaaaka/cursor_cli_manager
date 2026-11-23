package main

import (
	"bufio"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const healthPath = "/_ccm_proxy/health"
const drainTimeout = 3 * time.Second

type health struct {
	Proof      string  `json:"proof"`
	Phase      string  `json:"phase"`
	Generation uint64  `json:"generation"`
	Counters   traffic `json:"traffic"`
}

type traffic struct {
	HTTP    uint64 `json:"http"`
	CONNECT uint64 `json:"connect"`
}

func proof(owner, nonce string) string {
	mac := hmac.New(sha256.New, []byte(owner))
	mac.Write([]byte(nonce))
	return hex.EncodeToString(mac.Sum(nil))
}

var healthClient = &http.Client{
	Timeout:       400 * time.Millisecond,
	Transport:     &http.Transport{DialContext: (&net.Dialer{Timeout: 250 * time.Millisecond}).DialContext, DisableKeepAlives: true},
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
}

func localHealth(ctx context.Context, p profile) (health, error) {
	nonce, err := token()
	if err != nil {
		return health{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, proxyURL(p.Port)+healthPath, nil)
	if err != nil {
		return health{}, err
	}
	req.Header.Set("X-CCM-Challenge", nonce)
	resp, err := healthClient.Do(req)
	if err != nil {
		return health{}, err
	}
	defer resp.Body.Close()
	var h health
	if resp.StatusCode != http.StatusOK || json.NewDecoder(io.LimitReader(resp.Body, 1024)).Decode(&h) != nil ||
		!hmac.Equal([]byte(h.Proof), []byte(proof(p.Owner, nonce))) {
		return health{}, errors.New("local listener does not own this profile")
	}
	return h, nil
}

func probeRoute(ctx context.Context, port int, target string) error {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	addr := net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
	conn, err := (&net.Dialer{}).DialContext(ctx, "tcp", addr)
	if err != nil {
		return err
	}
	defer conn.Close()
	deadline, _ := ctx.Deadline()
	conn.SetDeadline(deadline)
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	if _, err := fmt.Fprintf(conn, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", target, target); err != nil {
		return err
	}
	response, err := http.ReadResponse(bufio.NewReader(io.LimitReader(conn, 8192)), &http.Request{Method: http.MethodConnect})
	if err != nil {
		return err
	}
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("CONNECT readiness check returned HTTP %d", response.StatusCode)
	}
	return nil
}

type generation struct {
	ctx    context.Context
	cancel context.CancelFunc
	addr   string
	conns  map[*trackedConn]struct{}
}

type relay struct {
	port         string
	owner        string
	via          string
	mu           sync.Mutex
	active       *generation
	retired      map[*generation]*time.Timer
	clients      map[net.Conn]struct{}
	closed       bool
	phase        string
	number       uint64
	probe        chan struct{}
	transport    *http.Transport
	httpCount    atomic.Uint64
	connectCount atomic.Uint64
}

func newRelay(port int, owner string) *relay {
	r := &relay{
		port: strconv.Itoa(port), owner: owner, phase: "starting",
		retired: make(map[*generation]*time.Timer), clients: make(map[net.Conn]struct{}), probe: make(chan struct{}, 1),
	}
	identity := sha256.Sum256([]byte(owner))
	r.via = "ccm-" + hex.EncodeToString(identity[:6])
	r.transport = &http.Transport{
		DialContext: r.dial, DisableKeepAlives: true, ResponseHeaderTimeout: 45 * time.Second,
		MaxResponseHeaderBytes: 1 << 20,
	}
	return r
}

func (r *relay) publish(phase, addr string, number uint64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return
	}
	r.phase, r.number = phase, number
	if r.active != nil && r.active.addr == addr {
		return
	}
	if g := r.active; g != nil {
		g.cancel()
		r.retired[g] = time.AfterFunc(drainTimeout, func() {
			r.mu.Lock()
			defer r.mu.Unlock()
			if _, exists := r.retired[g]; exists {
				for conn := range g.conns {
					conn.abort()
				}
				delete(r.retired, g)
			}
		})
	}
	r.active = nil
	if addr != "" {
		ctx, cancel := context.WithCancel(context.Background())
		r.active = &generation{ctx: ctx, cancel: cancel, addr: addr, conns: make(map[*trackedConn]struct{})}
	}
}

func (r *relay) close() {
	r.mu.Lock()
	r.closed = true
	if r.active != nil {
		r.active.cancel()
		for conn := range r.active.conns {
			conn.Conn.Close()
		}
	}
	for g, timer := range r.retired {
		timer.Stop()
		for conn := range g.conns {
			conn.Conn.Close()
		}
		delete(r.retired, g)
	}
	for conn := range r.clients {
		conn.Close()
	}
	r.mu.Unlock()
	r.transport.CloseIdleConnections()
}

type trackedConn struct {
	net.Conn
	relay      *relay
	generation *generation
	client     net.Conn // protected by relay.mu
}

func (c *trackedConn) abort() {
	c.Conn.Close()
	if c.client != nil {
		c.client.Close()
	}
}

func (c *trackedConn) Close() error {
	c.relay.mu.Lock()
	delete(c.generation.conns, c)
	c.relay.mu.Unlock()
	return c.Conn.Close()
}

func (c *trackedConn) CloseWrite() error { return c.Conn.(*net.TCPConn).CloseWrite() }

func (r *relay) dial(ctx context.Context, _, addr string) (net.Conn, error) {
	r.mu.Lock()
	g := r.active
	if g == nil || r.closed {
		r.mu.Unlock()
		return nil, errors.New("SSH backend is unavailable")
	}
	r.mu.Unlock()

	dialCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	stop := context.AfterFunc(g.ctx, cancel)
	defer stop()
	conn, err := socksOpen(dialCtx, g.addr, addr)
	if err != nil {
		if dialCtx.Err() == nil {
			select {
			case r.probe <- struct{}{}:
			default:
			}
		}
		return nil, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed || r.active != g || ctx.Err() != nil {
		conn.Close()
		return nil, errors.New("SSH backend changed during connection")
	}
	tracked := &trackedConn{Conn: conn, relay: r, generation: g}
	g.conns[tracked] = struct{}{}
	return tracked, nil
}

func stripHopHeaders(header http.Header) {
	for _, field := range header.Values("Connection") {
		for _, name := range strings.Split(field, ",") {
			header.Del(strings.TrimSpace(name))
		}
	}
	for _, name := range []string{"Connection", "Proxy-Connection", "Keep-Alive", "Proxy-Authenticate",
		"Proxy-Authorization", "Te", "Trailer", "Transfer-Encoding", "Upgrade"} {
		header.Del(name)
	}
}

func (r *relay) checkLoop(ctx context.Context, addr string) bool {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return true
	}
	n, err := strconv.Atoi(port)
	if err != nil || n < 1 || n > 65535 || host == "" {
		return true
	}
	if strconv.Itoa(n) != r.port {
		return false
	}
	name := strings.ToLower(strings.TrimSuffix(host, "."))
	if name == "localhost" || strings.HasSuffix(name, ".localhost") {
		return true
	}
	if ip := net.ParseIP(name); ip != nil {
		return ip.IsLoopback() || ip.IsUnspecified()
	}
	// Resolve only candidate loops at the gateway port. SOCKS gets the
	// original hostname and performs DNS on the SSH server for other traffic.
	probeCtx, cancel := context.WithTimeout(ctx, 250*time.Millisecond)
	defer cancel()
	ips, _ := net.DefaultResolver.LookupIPAddr(probeCtx, name)
	for _, ip := range ips {
		if ip.IP.IsLoopback() || ip.IP.IsUnspecified() {
			return true
		}
	}
	return false
}

func (r *relay) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	if req.Method == http.MethodGet && !req.URL.IsAbs() && req.URL.Path == healthPath {
		nonce := req.Header.Get("X-CCM-Challenge")
		if b, err := hex.DecodeString(nonce); err != nil || len(b) != 16 {
			http.NotFound(w, req)
			return
		}
		r.mu.Lock()
		h := health{Proof: proof(r.owner, nonce), Phase: r.phase, Generation: r.number}
		r.mu.Unlock()
		h.Counters = traffic{HTTP: r.httpCount.Load(), CONNECT: r.connectCount.Load()}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(h)
		return
	}
	if req.Method == http.MethodConnect {
		r.connect(w, req)
		return
	}
	if req.URL.Scheme != "http" || req.URL.Hostname() == "" || req.URL.User != nil {
		http.Error(w, "expected an HTTP proxy request", http.StatusBadRequest)
		return
	}
	port := req.URL.Port()
	if port == "" {
		port = "80"
	}
	via := strings.Join(req.Header.Values("Via"), ",")
	if len(via) > 4096 || strings.Count(via, ",") >= 12 ||
		strings.Contains(strings.ToLower(via), r.via) ||
		r.checkLoop(req.Context(), net.JoinHostPort(req.URL.Hostname(), port)) {
		http.Error(w, "proxy forwarding loop", http.StatusLoopDetected)
		return
	}

	out := req.Clone(req.Context())
	out.RequestURI, out.Host, out.Close = "", out.URL.Host, false
	stripHopHeaders(out.Header)
	out.Header.Del("X-CCM-Challenge")
	out.Header.Add("Via", "1.1 "+r.via)
	resp, err := r.transport.RoundTrip(out)
	if err != nil {
		http.Error(w, "SSH proxy could not reach destination", http.StatusBadGateway)
		return
	}
	r.httpCount.Add(1)
	defer resp.Body.Close()
	stripHopHeaders(resp.Header)
	for name, values := range resp.Header {
		w.Header()[name] = values
	}
	trailers := make([]string, 0, len(resp.Trailer))
	for name := range resp.Trailer {
		if !strings.EqualFold(name, "Content-Length") && !strings.EqualFold(name, "Transfer-Encoding") && !strings.EqualFold(name, "Trailer") {
			trailers = append(trailers, name)
		}
	}
	if len(trailers) != 0 {
		sort.Strings(trailers)
		w.Header().Set("Trailer", strings.Join(trailers, ", "))
	}
	w.WriteHeader(resp.StatusCode)
	if _, err := io.Copy(flushWriter{w}, resp.Body); err != nil {
		// Abort a partially delivered response instead of marking it complete.
		panic(http.ErrAbortHandler)
	}
	for _, name := range trailers {
		w.Header()[name] = resp.Trailer[name]
	}
}

type flushWriter struct{ http.ResponseWriter }

func (w flushWriter) Write(p []byte) (int, error) {
	n, err := w.ResponseWriter.Write(p)
	if f, ok := w.ResponseWriter.(http.Flusher); ok && err == nil {
		f.Flush()
	}
	return n, err
}

func (r *relay) connect(w http.ResponseWriter, req *http.Request) {
	if r.checkLoop(req.Context(), req.Host) {
		http.Error(w, "invalid or looping CONNECT target", http.StatusLoopDetected)
		return
	}
	remote, err := r.dial(req.Context(), "tcp", req.Host)
	if err != nil {
		http.Error(w, "SSH proxy could not reach destination", http.StatusBadGateway)
		return
	}
	defer remote.Close()
	client, rw, err := http.NewResponseController(w).Hijack()
	if err != nil {
		return
	}
	defer client.Close()

	r.mu.Lock()
	tracked := remote.(*trackedConn)
	if r.closed || r.active != tracked.generation {
		r.mu.Unlock()
		return
	}
	tracked.client = client
	r.clients[client] = struct{}{}
	r.mu.Unlock()
	defer func() { r.mu.Lock(); delete(r.clients, client); r.mu.Unlock() }()

	client.SetWriteDeadline(time.Now().Add(handshakeTimeout))
	if _, err := rw.WriteString("HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil {
		return
	}
	if err := rw.Flush(); err != nil {
		return
	}
	r.connectCount.Add(1)
	client.SetDeadline(time.Time{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		if _, err := io.Copy(remote, rw.Reader); err != nil {
			client.Close()
			remote.Close()
			return
		}
		tracked.CloseWrite()
	}()
	if _, err := io.Copy(client, remote); err != nil {
		client.Close()
		remote.Close()
	} else {
		client.(*net.TCPConn).CloseWrite()
	}
	<-done
}
