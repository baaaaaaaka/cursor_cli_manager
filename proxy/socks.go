package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"time"
)

const handshakeTimeout = 5 * time.Second

func socksOpen(ctx context.Context, addr, destination string) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(ctx, handshakeTimeout)
	defer cancel()
	var dialer net.Dialer
	conn, err := dialer.DialContext(ctx, "tcp", addr)
	if err != nil {
		return nil, err
	}
	var success bool
	defer func() {
		if !success {
			conn.Close()
		}
	}()
	deadline, _ := ctx.Deadline()
	if err := conn.SetDeadline(deadline); err != nil {
		return nil, err
	}
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	if _, err := conn.Write([]byte{5, 1, 0}); err != nil {
		return nil, err
	}
	var header [4]byte
	if _, err := io.ReadFull(conn, header[:2]); err != nil {
		return nil, err
	}
	if header[0] != 5 || header[1] != 0 {
		return nil, errors.New("invalid SOCKS5 greeting")
	}
	if destination == "" {
		return nil, nil
	}

	host, port, err := net.SplitHostPort(destination)
	if err != nil {
		return nil, err
	}
	n, err := strconv.Atoi(port)
	if err != nil || n < 1 || n > 65535 {
		return nil, errors.New("invalid proxy destination port")
	}
	request := []byte{5, 1, 0}
	if ip := net.ParseIP(host); ip != nil {
		if v4 := ip.To4(); v4 != nil {
			request = append(append(request, 1), v4...)
		} else {
			request = append(append(request, 4), ip.To16()...)
		}
	} else {
		if host == "" || len(host) > 255 {
			return nil, errors.New("invalid proxy destination hostname")
		}
		request = append(append(request, 3, byte(len(host))), host...)
	}
	request = append(request, byte(n>>8), byte(n))
	if _, err := conn.Write(request); err != nil {
		return nil, err
	}
	if _, err := io.ReadFull(conn, header[:]); err != nil {
		return nil, err
	}
	if header[0] != 5 || header[2] != 0 {
		return nil, errors.New("invalid SOCKS5 reply")
	}
	if header[1] != 0 {
		return nil, fmt.Errorf("SOCKS5 destination connection failed (code %d)", header[1])
	}
	size := 0
	switch header[3] {
	case 1:
		size = 4
	case 4:
		size = 16
	case 3:
		if _, err := io.ReadFull(conn, header[:1]); err != nil {
			return nil, err
		}
		size = int(header[0])
		if size == 0 {
			return nil, errors.New("invalid SOCKS5 reply address")
		}
	default:
		return nil, errors.New("invalid SOCKS5 reply address type")
	}
	var bound [257]byte
	if _, err := io.ReadFull(conn, bound[:size+2]); err != nil {
		return nil, err
	}
	if !stop() || ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if err := conn.SetDeadline(time.Time{}); err != nil {
		return nil, err
	}
	success = true
	return conn, nil
}
