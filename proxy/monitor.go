package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

// A guardian owns the SSH process group or Windows job. The worker keeps the
// guardian's stdin pipe open. EOF also arrives if the worker is killed.
type sshChild struct {
	release *os.File
	done    chan struct{}
	err     error
}

func startGuardian(s *store, p profile, addr string) (*sshChild, error) {
	exe, err := os.Executable()
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(exe, "--state-dir", s.dir, "guard-ssh", p.Name, p.Owner, addr)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	return startGuardianProcess(cmd, nil)
}

func startGuardianProcess(cmd *exec.Cmd, payload []byte) (*sshChild, error) {
	read, write, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	defer read.Close()
	detach(cmd)
	cmd.Stdin = read
	if err := cmd.Start(); err != nil {
		write.Close()
		return nil, err
	}
	child := &sshChild{release: write, done: make(chan struct{})}
	go func() {
		child.err = cmd.Wait()
		close(child.done)
	}()
	if len(payload) != 0 {
		if _, err := write.Write(payload); err != nil {
			return nil, errors.Join(err, child.close())
		}
	}
	return child, nil
}

func (c *sshChild) close() error {
	c.release.Close()
	select {
	case <-c.done:
		if c.err != nil {
			// The guardian exits unsuccessfully only when it cannot start or
			// reap SSH. The worker never assumes success after a cleanup error.
			if exit, ok := c.err.(*exec.ExitError); ok && exit.ExitCode() == 2 {
				return c.err
			}
		}
		return nil
	case <-time.After(6 * time.Second):
		return errors.New("SSH guardian did not confirm shutdown")
	}
}

var errSSHCleanup = errors.New("SSH process cleanup failed")

func guardSSH(s *store, name, owner, addr string) error {
	host, port, err := net.SplitHostPort(addr)
	if err != nil || host != "127.0.0.1" {
		return errors.New("invalid SOCKS listener address")
	}
	if n, err := strconv.Atoi(port); err != nil || n <= 0 || n > 65535 {
		return errors.New("invalid SOCKS listener port")
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	go func() {
		io.Copy(io.Discard, os.Stdin)
		cancel()
	}()
	check, end := context.WithTimeout(ctx, 3*time.Second)
	defer end()
	p, err := s.get(check, name)
	if err != nil {
		return err
	}
	if p.Owner != owner || owner == "" || !p.Desired {
		return errOwnership
	}
	lock := s.lock(p.ID + "-ssh")
	defer lock.Close()
	ok, err := lock.TryRLock()
	if err != nil {
		return err
	}
	if !ok {
		return errors.New("profile is changing SSH ownership")
	}
	current, err := s.get(check, name)
	if err != nil {
		return err
	}
	if current.ID != p.ID || current.Owner != owner || !current.Desired {
		return errOwnership
	}

	ssh, removeConfig, err := runSSH(p, addr)
	if err != nil {
		return err
	}
	defer removeConfig()
	var stopped bool
	select {
	case <-ctx.Done():
		stopped = true
	case <-ssh.done:
	}
	if err := ssh.close(); err != nil {
		saveCleanupFailure(s, p, err.Error())
		return fmt.Errorf("%w: %v", errSSHCleanup, err)
	}
	if stopped {
		return nil
	}
	return errors.New("SSH exited")
}

func monitor(s *store, name, owner string) error {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	p, err := s.get(ctx, name)
	if err != nil {
		return err
	}
	if p.Owner != owner || owner == "" || !p.Desired {
		return errOwnership
	}
	lock := s.lock(p.ID + "-monitor")
	defer lock.Close()
	free, err := lock.TryLock()
	if err != nil {
		return err
	}
	if !free {
		return errors.New("another monitor owns this proxy")
	}

	exe, err := os.Executable()
	if err != nil {
		return err
	}
	for ctx.Err() == nil {
		current, err := s.get(ctx, p.Name)
		if err != nil {
			return err
		}
		if current.ID != p.ID || current.Owner != p.Owner || !current.Desired {
			return nil
		}
		if current.Phase == "blocked" {
			return errors.New(current.Problem)
		}

		cmd := exec.Command(exe, "--state-dir", s.dir, "serve", name, p.Owner)
		detach(cmd)
		cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
		if err := cmd.Start(); err == nil {
			done := make(chan error, 1)
			go func() { done <- cmd.Wait() }()
			select {
			case err = <-done:
			case <-ctx.Done():
				// Foreground interruption uses the same desired state as stop.
				requestCtx, end := context.WithTimeout(context.Background(), 3*time.Second)
				_, requestErr := updateOwned(requestCtx, s, p, func(current *profile) {
					current.Desired, current.Phase = false, "stopping"
					current.StopSeq++
				})
				end()
				if requestErr != nil && !errors.Is(requestErr, errOwnership) {
					cmd.Process.Kill()
				}
				select {
				case <-done:
				case <-time.After(8 * time.Second):
					cmd.Process.Kill()
					<-done
				}
				lock.Close()
				finish, end := context.WithTimeout(context.Background(), 8*time.Second)
				defer end()
				_, err := stop(finish, s, p.Name)
				return err
			}
			if err != nil {
				fmt.Fprintln(os.Stderr, err)
			}
		} else {
			fmt.Fprintln(os.Stderr, err)
		}
		// An exec wait confirmed the old worker exited. Its pipe guardian can
		// still be reaping SSH, so wait for that lock before replacing the owner.
		check, end := context.WithTimeout(ctx, 6*time.Second)
		guard := s.lock(p.ID + "-ssh")
		free, err := guard.TryLockContext(check, 100*time.Millisecond)
		end()
		if err != nil || !free {
			guard.Close()
			saveCleanupFailure(s, p, "could not verify SSH cleanup after gateway exit")
			return errors.New("could not verify SSH cleanup after gateway exit")
		}
		newOwner, err := token()
		if err != nil {
			guard.Close()
			return err
		}
		p, err = updateOwned(ctx, s, p, func(current *profile) {
			if current.Phase == "blocked" {
				return
			}
			current.WorkerFailures++
			current.PID, current.Phase = 0, "starting"
			current.Deadline = time.Now().Add(startLease)
			if current.WorkerFailures >= 4 {
				current.Phase, current.Problem = "blocked", "gateway restart limit reached"
			} else {
				current.Owner, current.Problem = newOwner, "gateway exited and is restarting"
			}
		})
		guard.Close()
		if errors.Is(err, errOwnership) {
			return nil
		}
		if err != nil {
			return err
		}
		if p.Phase != "blocked" {
			if err := wait(ctx, retryDelay(p.WorkerFailures)); err != nil {
				return nil
			}
		}
	}
	return nil
}
