package main

import (
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestSSHProcessStopsDescendant(t *testing.T) {
	portFile := filepath.Join(t.TempDir(), "endpoint")
	cmd := exec.Command(os.Args[0], "-test.run=^TestSSHProcessFixture$")
	cmd.Env = append(os.Environ(), "CCM_PROXY_CHILD_ROLE=parent", "CCM_PROXY_CHILD_ENDPOINT="+portFile)
	p, err := startSSHProcess(cmd)
	if err != nil {
		t.Fatal(err)
	}
	var closed bool
	t.Cleanup(func() {
		if !closed {
			p.close()
		}
	})
	var endpoint []byte
	deadline := time.Now().Add(6 * time.Second)
	for time.Now().Before(deadline) {
		endpoint, _ = os.ReadFile(portFile)
		if len(endpoint) > 0 {
			break
		}
		select {
		case <-p.done:
			t.Fatal("process fixture exited")
		case <-time.After(25 * time.Millisecond):
		}
	}
	if len(endpoint) == 0 {
		t.Fatal("descendant did not listen")
	}
	conn, err := net.DialTimeout("tcp", string(endpoint), 500*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	conn.Close()
	err = p.close()
	closed = true
	if err != nil {
		t.Fatal(err)
	}
	if conn, err := net.DialTimeout("tcp", string(endpoint), 500*time.Millisecond); err == nil {
		conn.Close()
		t.Fatal("SSH descendant survived shutdown")
	}
}

func TestSSHProcessClosesAfterChildExit(t *testing.T) {
	cmd := exec.Command(os.Args[0], "-test.run=^$")
	proc, err := startSSHProcess(cmd)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-proc.done:
	case <-time.After(5 * time.Second):
		proc.close()
		t.Fatal("did not observe child exit")
	}
	if err := proc.close(); err != nil {
		t.Fatal(err)
	}
}

func TestSSHProcessFixture(t *testing.T) {
	endpoint := os.Getenv("CCM_PROXY_CHILD_ENDPOINT")
	switch os.Getenv("CCM_PROXY_CHILD_ROLE") {
	case "parent":
		cmd := exec.Command(os.Args[0], "-test.run=^TestSSHProcessFixture$")
		cmd.Env = append(os.Environ(), "CCM_PROXY_CHILD_ROLE=descendant")
		if err := cmd.Run(); err != nil {
			t.Fatal(err)
		}
	case "descendant":
		ln, err := net.Listen("tcp4", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		defer ln.Close()
		if err := os.WriteFile(endpoint, []byte(ln.Addr().String()), 0600); err != nil {
			t.Fatal(err)
		}
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			conn.Close()
		}
	}
}
