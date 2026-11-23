package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// The direct SSH endpoint is useful for recovery admission. A ProxyJump or
// ProxyCommand may reach a private hostname, so probing it directly would
// give the wrong result and could bypass the user's SSH configuration.
func directEndpoint(config string) string {
	values := make(map[string]string)
	for _, line := range strings.Split(config, "\n") {
		parts := strings.SplitN(strings.TrimSpace(line), " ", 2)
		if len(parts) == 2 {
			values[parts[0]] = strings.TrimSpace(parts[1])
		}
	}
	for _, key := range []string{"proxyjump", "proxycommand"} {
		if value := values[key]; value != "" && value != "none" {
			return ""
		}
	}
	host := values["hostname"]
	port, err := strconv.Atoi(values["port"])
	if !hostPattern.MatchString(host) || err != nil || port < 1 || port > 65535 {
		return ""
	}
	return net.JoinHostPort(host, strconv.Itoa(port))
}

func effectiveSSHAddress(ctx context.Context, p profile) string {
	probe, end := context.WithTimeout(ctx, 2*time.Second)
	defer end()
	cmd := exec.Command("ssh", append([]string{"-G"}, sshTargetArgs(p)...)...)
	var out bytes.Buffer
	cmd.Stdout = &out
	child, err := startSSHProcess(cmd)
	if err != nil {
		return ""
	}
	select {
	case <-probe.Done():
	case <-child.done:
	}
	if err := child.close(); err != nil || probe.Err() != nil {
		return ""
	}
	return directEndpoint(out.String())
}

func probeNetwork(ctx context.Context, address string) error {
	if address != "" {
		probe, end := context.WithTimeout(ctx, time.Second)
		defer end()
		conn, err := (&net.Dialer{}).DialContext(probe, "tcp", address)
		if err != nil {
			return fmt.Errorf("SSH endpoint %s is unavailable: %w", address, err)
		}
		return conn.Close()
	}

	interfaces, err := net.Interfaces()
	if err != nil {
		return err
	}
	for _, iface := range interfaces {
		if iface.Flags&net.FlagUp != 0 && iface.Flags&net.FlagLoopback == 0 {
			return nil
		}
	}
	return errors.New("no active network interface for the configured SSH route")
}

func waitForNetwork(ctx context.Context, address string, stalled func(error) error) error {
	for delay := 250 * time.Millisecond; ; delay = min(2*delay, 15*time.Second) {
		err := probeNetwork(ctx, address)
		if err == nil {
			return nil
		}
		if delay == 250*time.Millisecond {
			if err := stalled(err); err != nil {
				return err
			}
		}
		if err := wait(ctx, delay); err != nil {
			return err
		}
	}
}
