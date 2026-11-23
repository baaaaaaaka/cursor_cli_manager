package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

const maxSSHFailures = 6

func sshTargetArgs(p profile) []string {
	if p.SSHConfig != "" {
		return []string{"-F", p.SSHConfig, "--", p.Host}
	}
	destination := p.Host
	if p.User != "" {
		destination = p.User + "@" + destination
	}
	if p.SSHPort != 0 {
		return []string{"-p", strconv.Itoa(p.SSHPort), "--", destination}
	}
	return []string{"--", destination}
}

func runSSH(p profile, socksAddr string) (*process, func(), error) {
	return runSSHWithOutput(p, socksAddr, os.Stdout, os.Stderr)
}

func runSSHWithOutput(p profile, socksAddr string, stdout, stderr io.Writer) (*process, func(), error) {
	p, cleanup, err := prepareSSHConfig(p)
	if err != nil {
		return nil, nil, err
	}
	args := []string{
		"-N", "-T", "-a", "-x",
		"-o", "BatchMode=yes",
		"-o", "StrictHostKeyChecking=yes",
		"-o", "ExitOnForwardFailure=yes",
		"-o", "ConnectTimeout=6",
		"-o", "ServerAliveInterval=5",
		"-o", "ServerAliveCountMax=2",
		"-o", "PermitLocalCommand=no",
		"-o", "ControlMaster=no",
		"-o", "ControlPath=none",
		"-o", "ForkAfterAuthentication=no",
		"-D", socksAddr,
	}
	args = append(args, sshTargetArgs(p)...)
	cmd := exec.Command("ssh", args...)
	cmd.Stdout, cmd.Stderr = stdout, stderr
	child, err := startSSHProcess(cmd)
	if err != nil {
		cleanup()
		return nil, nil, err
	}
	return child, cleanup, nil
}

func randomSOCKSAddress() (string, error) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	addr := listener.Addr().String()
	return addr, listener.Close()
}

func waitForSSH(ctx context.Context, child *sshChild, addr, target string) error {
	ctx, cancel := context.WithTimeout(ctx, 9*time.Second)
	defer cancel()
	var last error
	for ctx.Err() == nil {
		select {
		case <-child.done:
			return errors.New("ssh exited before the SOCKS listener was ready")
		default:
		}
		conn, err := socksOpen(ctx, addr, target)
		if err == nil {
			if conn != nil {
				conn.Close()
			}
			select {
			case <-child.done:
				return errors.New("ssh exited during readiness probe")
			default:
				return nil
			}
		}
		last = err
		if err := wait(ctx, 200*time.Millisecond); err != nil {
			break
		}
	}
	return fmt.Errorf("SSH readiness probe failed: %w", errors.Join(ctx.Err(), last))
}

func monitorOwnership(ctx context.Context, s *store, p profile, cancel context.CancelFunc) {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	var lastOK = time.Now()
	var lastWrite time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		checkCtx, end := context.WithTimeout(ctx, time.Second)
		var err error
		if time.Since(lastWrite) > 3*time.Second {
			_, err = updateOwned(checkCtx, s, p, func(current *profile) {
				current.Deadline = time.Now().Add(startLease)
			})
			if err == nil {
				lastWrite = time.Now()
			}
		} else {
			var current profile
			current, err = s.get(checkCtx, p.Name)
			if err == nil && (current.ID != p.ID || current.Owner != p.Owner || !current.Desired) {
				err = errOwnership
			}
		}
		end()
		if err == nil {
			lastOK = time.Now()
		} else if errors.Is(err, errOwnership) || errors.Is(err, errMissing) || time.Since(lastOK) > startLease {
			cancel()
			return
		}
	}
}

func serve(s *store, name, owner string) error {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	checkCtx, end := context.WithTimeout(ctx, 3*time.Second)
	p, err := s.get(checkCtx, name)
	end()
	if err != nil {
		return err
	}
	if p.Owner == "" || p.Owner != owner || !p.Desired {
		return errOwnership
	}

	lock := s.lock(p.ID)
	defer lock.Close()
	free, err := lock.TryLock()
	if err != nil {
		return err
	}
	if !free {
		return errors.New("another worker owns this proxy")
	}
	checkCtx, end = context.WithTimeout(ctx, 3*time.Second)
	_, err = updateOwned(checkCtx, s, p, func(current *profile) { current.PID = os.Getpid() })
	end()
	if err != nil {
		return err
	}

	listener, err := net.Listen("tcp4", net.JoinHostPort("127.0.0.1", strconv.Itoa(p.Port)))
	if err != nil {
		saveProblem(s, p, "blocked", fmt.Sprintf("cannot bind saved public port %d: %v", p.Port, err))
		return err
	}
	defer listener.Close()
	p.Port = listener.Addr().(*net.TCPAddr).Port
	checkCtx, end = context.WithTimeout(ctx, 3*time.Second)
	_, err = updateOwned(checkCtx, s, p, func(current *profile) { current.Port = p.Port })
	end()
	if err != nil {
		return err
	}

	r := newRelay(p.Port, p.Owner)
	server := &http.Server{
		Handler: r, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 45 * time.Second,
		MaxHeaderBytes: 1 << 16,
		BaseContext:    func(net.Listener) context.Context { return ctx },
	}
	go func() {
		if err := server.Serve(listener); !errors.Is(err, http.ErrServerClosed) {
			fmt.Fprintln(os.Stderr, err)
			cancel()
		}
	}()
	defer server.Close()
	defer r.close()

	go monitorOwnership(ctx, s, p, cancel)
	err = supervise(ctx, s, p, r)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
	}
	return err
}

func saveProblem(s *store, p profile, phase, problem string) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if _, err := updateOwned(ctx, s, p, func(current *profile) {
		current.Phase, current.Problem = phase, problem
		current.Deadline = time.Now()
	}); err != nil && !errors.Is(err, errOwnership) {
		fmt.Fprintln(os.Stderr, err)
	}
}

func saveCleanupFailure(s *store, p profile, problem string) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_, err := s.update(ctx, p.Name, func(current *profile) error {
		if current.ID != p.ID || current.Owner != p.Owner {
			return errOwnership
		}
		current.CleanupFailed, current.Problem = true, problem
		if current.Desired {
			current.Phase = "blocked"
		}
		return nil
	})
	if err != nil && !errors.Is(err, errOwnership) {
		fmt.Fprintln(os.Stderr, err)
	}
}

func retryDelay(failures int) time.Duration {
	if failures <= 0 {
		return 0
	}
	return time.Second * time.Duration(1<<min(failures-1, 3))
}

type backend struct {
	addr  string
	child *sshChild
}

func (b backend) close() error {
	if b.child == nil {
		return nil
	}
	return b.child.close()
}

func supervise(ctx context.Context, s *store, p profile, r *relay) (result error) {
	var active backend
	var networkAddress string
	var resolvedNetworkAddress bool
	closeBackend := func(b backend) error {
		if err := b.close(); err != nil {
			saveCleanupFailure(s, p, err.Error())
			return err
		}
		return nil
	}
	defer func() {
		r.publish("recovering", "", p.Generation)
		result = errors.Join(result, closeBackend(active))
	}()
	for ctx.Err() == nil {
		if p.Failures >= maxSSHFailures {
			saveProblem(s, p, "blocked", p.Problem)
			return fmt.Errorf("SSH recovery limit reached: %s", p.Problem)
		}
		if delay := time.Until(p.RetryAt); delay > 0 {
			// Do not let a wall-clock correction create a long retry delay.
			if err := wait(ctx, min(delay, retryDelay(p.Failures))); err != nil {
				return nil
			}
		}
		if p.Failures > 0 {
			if !resolvedNetworkAddress {
				networkAddress = effectiveSSHAddress(ctx, p)
				resolvedNetworkAddress = true
			}
			err := waitForNetwork(ctx, networkAddress, func(cause error) error {
				r.publish("waiting-network", active.addr, p.Generation)
				_, err := updateOwned(ctx, s, p, func(current *profile) {
					current.Phase, current.Problem = "waiting-network", cause.Error()
				})
				return err
			})
			if err != nil {
				if ctx.Err() != nil || errors.Is(err, errOwnership) {
					return nil
				}
				return err
			}
		}
		var candidate backend
		var err error
		candidate.addr, err = randomSOCKSAddress()
		if err == nil {
			candidate.child, err = startGuardian(s, p, candidate.addr)
		}
		if err == nil {
			err = waitForSSH(ctx, candidate.child, candidate.addr, p.Target)
		}
		if err == nil {
			_, err = updateOwned(ctx, s, p, func(current *profile) {
				current.Phase, current.Problem = "ready", ""
				current.Generation = p.Generation + 1
			})
		}
		if err == nil {
			p.Generation++
			r.publish("ready", candidate.addr, p.Generation)
			old := active
			active = candidate
			if old.child != nil {
				// The old generation remains usable while the candidate starts.
				// Once swapped, existing streams get a bounded drain interval.
				wait(ctx, drainTimeout)
				if err := closeBackend(old); err != nil {
					return err
				}
			}
			err = watchSSH(ctx, active, p.Target, r.probe, func() error {
				if p.Failures == 0 && p.WorkerFailures == 0 {
					return nil
				}
				_, err := updateOwned(ctx, s, p, func(current *profile) {
					current.Failures, current.WorkerFailures = 0, 0
					current.RetryAt = time.Time{}
				})
				if err == nil {
					p.Failures, p.WorkerFailures = 0, 0
					p.RetryAt = time.Time{}
				}
				return err
			})
		} else if err := closeBackend(candidate); err != nil {
			return err
		}
		if active.child != nil {
			select {
			case <-active.child.done:
				if err := closeBackend(active); err != nil {
					return err
				}
				active = backend{}
			default:
			}
		}
		r.publish("recovering", active.addr, p.Generation)
		if ctx.Err() != nil || errors.Is(err, errOwnership) {
			return nil
		}
		p.Failures++
		p.RetryAt = time.Now().Add(retryDelay(p.Failures))
		p.Problem = err.Error()
		fmt.Fprintln(os.Stderr, p.Problem)
		if _, err := updateOwned(ctx, s, p, func(current *profile) {
			current.Phase, current.Problem = "recovering", p.Problem
			current.Failures, current.WorkerFailures, current.RetryAt = p.Failures, p.WorkerFailures, p.RetryAt
		}); err != nil {
			return err
		}
	}
	return nil
}

func watchSSH(ctx context.Context, active backend, target string, request <-chan struct{}, stable func() error) error {
	tick := time.NewTicker(3 * time.Second)
	defer tick.Stop()
	settled := time.NewTimer(45 * time.Second)
	defer settled.Stop()
	var lastCheck time.Time
	var lastWall = time.Now().UnixNano()
	var failureAt time.Time
	for {
		var requested bool
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-active.child.done:
			return errors.New("ssh process exited")
		case <-settled.C:
			if err := stable(); err != nil {
				return err
			}
		case <-tick.C:
		case <-request:
			if time.Since(lastCheck) < time.Second {
				continue
			}
			requested = true
		}
		now := time.Now().UnixNano()
		resumed := now-lastWall > int64(8*time.Second) || now < lastWall
		lastWall, lastCheck = now, time.Now()
		probeCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		_, err := socksOpen(probeCtx, active.addr, "")
		cancel()
		if err != nil {
			return fmt.Errorf("SOCKS listener failed health probe: %w", err)
		}
		if (resumed || requested) && target != "" {
			// A local greeting can succeed when the remote route is unavailable.
			// Confirm request-triggered failures against the independent target.
			probeCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
			conn, err := socksOpen(probeCtx, active.addr, target)
			cancel()
			if err == nil {
				failureAt = time.Time{}
				conn.Close()
			} else {
				if resumed || (!failureAt.IsZero() && time.Since(failureAt) < 10*time.Second) {
					return fmt.Errorf("configured route probe failed: %w", err)
				}
				failureAt = time.Now()
			}
		}
	}
}
