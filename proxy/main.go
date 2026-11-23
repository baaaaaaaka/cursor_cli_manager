package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/gofrs/flock"
)

const (
	startLease        = 12 * time.Second
	managementTimeout = 55 * time.Second
)

var namePattern = regexp.MustCompile("^[[:alnum:]_][[:alnum:]_.-]{0,63}$")
var hostPattern = regexp.MustCompile("^[[:alnum:]_.:%-]+$")
var userPattern = regexp.MustCompile("^[[:alnum:]_.-]+$")

type status struct {
	profile
	URL      string   `json:"url,omitempty"`
	Verified bool     `json:"verified"`
	Traffic  *traffic `json:"traffic,omitempty"`
}

func describe(ctx context.Context, p profile) status {
	s := status{profile: p}
	if p.Port == 0 || p.Owner == "" || !p.Desired {
		return s
	}
	h, err := localHealth(ctx, p)
	if err != nil {
		if p.Phase == "ready" {
			s.Phase = "unreachable"
			s.Problem = err.Error()
		}
		return s
	}
	s.Verified = true
	s.Phase = h.Phase
	s.Generation = h.Generation
	s.Traffic = &h.Counters
	if h.Phase == "ready" {
		s.URL = proxyURL(p.Port)
	}
	return s
}

func proxyURL(port int) string { return "http://" + net.JoinHostPort("127.0.0.1", strconv.Itoa(port)) }

func wait(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func leaseCurrent(p profile, now time.Time) bool {
	remaining := p.Deadline.Sub(now)
	return remaining > 0 && remaining <= 2*startLease
}

func launch(s *store, p profile, foreground bool) error {
	exe, err := residentHelper(s.dir)
	if err != nil {
		return err
	}
	cmd := exec.Command(exe, "--state-dir", s.dir, "monitor", p.Name, p.Owner)
	if foreground {
		cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stderr, os.Stderr
		return cmd.Run()
	}
	logfile, err := os.OpenFile(filepath.Join(s.dir, p.ID+".log"), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0600)
	if err != nil {
		return err
	}
	defer logfile.Close()
	detach(cmd)
	cmd.Stdout, cmd.Stderr = logfile, logfile
	if err := cmd.Start(); err != nil {
		return err
	}
	// The child is detached from the invoking terminal and manages its own lifetime.
	go cmd.Wait()
	return nil
}

// Use the same lock order wherever a record can be reassigned or removed.
func holdOwnerLocks(ctx context.Context, s *store, id string, waitForRelease bool) (func(), bool, error) {
	monitor, worker, ssh := s.lock(id+"-monitor"), s.lock(id), s.lock(id+"-ssh")
	release := func() { ssh.Close(); worker.Close(); monitor.Close() }
	for _, lock := range []*flock.Flock{monitor, worker, ssh} {
		var ok bool
		var err error
		if waitForRelease {
			ok, err = lock.TryLockContext(ctx, 100*time.Millisecond)
		} else {
			ok, err = lock.TryLock()
		}
		if !ok || err != nil {
			release()
			return nil, false, err
		}
	}
	return release, true, nil
}

func start(ctx context.Context, s *store, name string, foreground bool, expectedID ...string) (status, error) {
	var observedID string
	var observedStop uint64
	for ctx.Err() == nil {
		p, err := s.get(ctx, name)
		if err != nil {
			return status{}, err
		}
		if len(expectedID) != 0 && p.ID != expectedID[0] {
			return status{}, errors.New("selected proxy profile changed; run ccm proxy init")
		}
		if observedID == "" {
			observedID = p.ID
			observedStop = p.StopSeq
		}
		if p.ID != observedID || p.StopSeq != observedStop || p.Phase == "stopping" {
			return status{}, errors.New("proxy profile changed or is stopping")
		}
		if p.Phase == "blocked" {
			return status{}, fmt.Errorf("proxy is blocked: %s", p.Problem)
		}
		result := describe(ctx, p)
		if result.URL != "" {
			if foreground {
				return status{}, errors.New("proxy is already running")
			}
			if p.Target != "" {
				if err := probeRoute(ctx, p.Port, p.Target); err != nil {
					return status{}, fmt.Errorf("proxy route target %s: %w", p.Target, err)
				}
			}
			// Stop can change desired state while the route probe is in flight.
			current, err := s.get(ctx, name)
			if err != nil {
				return status{}, err
			}
			if !current.Desired || current.Phase == "stopping" || current.ID != p.ID || current.StopSeq != observedStop {
				return status{}, errOwnership
			}
			if current.Owner != p.Owner || current.Phase != "ready" || current.Generation != result.Generation {
				continue
			}
			return result, nil
		}
		if p.Owner != "" && leaseCurrent(p, time.Now()) {
			if err := wait(ctx, 150*time.Millisecond); err != nil {
				break
			}
			continue
		}

		release, free, err := holdOwnerLocks(ctx, s, p.ID, false)
		if err != nil {
			return status{}, err
		}
		if !free {
			if err := wait(ctx, 150*time.Millisecond); err != nil {
				break
			}
			continue
		}
		if _, err := exec.LookPath("ssh"); err != nil {
			release()
			return status{}, errors.New("OpenSSH client was not found on PATH")
		}

		newOwner, err := token()
		if err != nil {
			release()
			return status{}, err
		}
		if p.Desired && p.Owner != "" {
			p.WorkerFailures++
			if p.WorkerFailures >= 4 {
				p.Phase = "blocked"
				p.Problem = "gateway restart limit reached"
				ok, err := s.change(ctx, p)
				release()
				if err != nil {
					return status{}, err
				}
				if ok {
					return status{}, errors.New(p.Problem)
				}
				continue
			}
		}
		p.Desired, p.Owner, p.Phase = true, newOwner, "starting"
		p.PID, p.Problem = 0, ""
		p.Deadline = time.Now().Add(startLease)
		ok, err := s.change(ctx, p)
		release()
		if err != nil {
			return status{}, err
		}
		if !ok {
			continue
		}
		if err := launch(s, p, foreground); err != nil {
			updateOwned(ctx, s, p, func(current *profile) {
				current.Deadline = time.Now()
				current.Problem = err.Error()
			})
			return status{}, err
		}
		if foreground {
			return status{}, nil
		}
	}
	return status{}, fmt.Errorf("proxy did not become ready: %w", ctx.Err())
}

var errOwnership = errors.New("proxy ownership changed")

func updateOwned(ctx context.Context, s *store, p profile, edit func(*profile)) (profile, error) {
	return s.update(ctx, p.Name, func(current *profile) error {
		if current.ID != p.ID || current.Owner != p.Owner || !current.Desired {
			return errOwnership
		}
		edit(current)
		return nil
	})
}

func stop(ctx context.Context, s *store, name string) (profile, error) {
	p, err := s.get(ctx, name)
	if err != nil {
		return profile{}, err
	}
	original := p.ID
	p, err = s.update(ctx, name, func(p *profile) error {
		if p.ID != original {
			return errOwnership
		}
		if p.CleanupFailed {
			return fmt.Errorf("SSH cleanup could not be verified: %s", p.Problem)
		}
		p.Desired, p.Phase = false, "stopping"
		p.StopSeq++
		return nil
	})
	if err != nil {
		return profile{}, err
	}
	release, free, err := holdOwnerLocks(ctx, s, p.ID, true)
	if err != nil {
		return profile{}, err
	}
	if !free {
		return profile{}, errors.New("proxy worker did not stop")
	}
	defer release()
	result, err := s.update(ctx, name, func(current *profile) error {
		if current.ID != p.ID || current.Desired {
			return errOwnership
		}
		if current.CleanupFailed {
			return fmt.Errorf("SSH cleanup could not be verified: %s", current.Problem)
		}
		current.Owner, current.PID, current.Phase = "", 0, "stopped"
		current.Deadline = time.Time{}
		current.RetryAt = time.Time{}
		current.Failures, current.WorkerFailures, current.Problem = 0, 0, ""
		return nil
	})
	return result, err
}

func prune(ctx context.Context, s *store) ([]string, error) {
	items, err := s.list(ctx)
	if err != nil {
		return nil, err
	}
	reaped := make([]string, 0)
	for _, p := range items {
		if p.Owner == "" || p.Phase == "blocked" || p.Phase == "stopping" || leaseCurrent(p, time.Now()) {
			continue
		}
		release, free, err := holdOwnerLocks(ctx, s, p.ID, false)
		if err != nil {
			return nil, err
		}
		if !free {
			continue
		}
		p.Owner, p.PID, p.Phase = "", 0, "stopped"
		p.Desired = false
		p.Deadline = time.Time{}
		ok, err := s.change(ctx, p)
		release()
		if err != nil {
			return nil, err
		}
		if ok {
			reaped = append(reaped, p.Name)
		}
	}
	return reaped, nil
}

func parseTarget(target string) error {
	if target == "" {
		return nil
	}
	host, port, err := net.SplitHostPort(target)
	if err != nil || host == "" || !hostPattern.MatchString(host) {
		return errors.New("route target must be host:port")
	}
	n, err := strconv.Atoi(port)
	if err != nil || n < 1 || n > 65535 {
		return errors.New("invalid route target port")
	}
	return nil
}

func parseProfile(args []string) (profile, error) {
	if len(args) == 0 || !namePattern.MatchString(args[0]) {
		return profile{}, errors.New("provide a valid profile name")
	}
	fs := flag.NewFlagSet("profile add", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	p := profile{Name: args[0], Phase: "stopped"}
	fs.StringVar(&p.Host, "host", "", "OpenSSH host or alias")
	fs.StringVar(&p.User, "user", "", "SSH user")
	fs.IntVar(&p.SSHPort, "port", 0, "SSH server port")
	fs.StringVar(&p.SSHConfig, "ssh-config", "", "Absolute OpenSSH configuration path")
	fs.StringVar(&p.Target, "route-target", "", "Optional destination host:port for readiness")
	if err := fs.Parse(args[1:]); err != nil {
		return profile{}, err
	}
	if p.Host == "" && fs.NArg() == 1 {
		p.Host = fs.Arg(0)
	} else if fs.NArg() != 0 {
		return profile{}, errors.New("unexpected profile arguments")
	}
	p.Host = strings.TrimPrefix(strings.TrimSuffix(p.Host, "]"), "[")
	if !hostPattern.MatchString(p.Host) || strings.HasPrefix(p.Host, "-") || (p.User != "" && !userPattern.MatchString(p.User)) || p.SSHPort < 0 || p.SSHPort > 65535 {
		return profile{}, errors.New("provide a valid SSH host, user and port")
	}
	if p.SSHConfig != "" && (!filepath.IsAbs(p.SSHConfig) || p.User != "" || p.SSHPort != 0) {
		return profile{}, errors.New("--ssh-config requires an absolute path and uses the SSH config user and port")
	}
	if err := parseTarget(p.Target); err != nil {
		return profile{}, err
	}
	var err error
	p.ID, err = token()
	return p, err
}

func run(dir string, args []string) (any, error) {
	if len(args) == 0 {
		return nil, errors.New("use profile add/list/remove, start, list, stop, prune, reset or doctor")
	}
	ctx, cancel := context.WithTimeout(context.Background(), managementTimeout)
	defer cancel()
	group := args[0] == "profile"
	if group {
		args = args[1:]
	}
	if len(args) == 0 {
		return nil, errors.New("use profile add/list/remove")
	}
	cmd, params := args[0], args[1:]
	if group && cmd == "list" && len(params) == 1 && params[0] == "--setup" {
		return runSetup(ctx, dir, []string{"status"})
	}
	if !group && cmd == "setup" {
		return runSetup(ctx, dir, params)
	}
	if group && cmd != "add" && cmd != "list" && cmd != "remove" {
		return nil, errors.New("use profile add/list/remove")
	}
	readOnly := cmd == "list" || cmd == "doctor"
	s, err := openStore(ctx, dir, !readOnly)
	if errors.Is(err, errMissing) && cmd == "list" {
		return []profile{}, nil
	}
	if err != nil {
		return nil, err
	}
	defer s.db.Close()
	switch cmd {
	case "add":
		if !group {
			break
		}
		p, err := parseProfile(params)
		if err != nil {
			return nil, err
		}
		return p, s.add(ctx, p)
	case "list":
		if len(params) != 0 {
			break
		}
		items, err := s.list(ctx)
		if err != nil || group {
			return items, err
		}
		results := make([]status, 0, len(items))
		for _, p := range items {
			results = append(results, describe(ctx, p))
		}
		return results, nil
	case "prune":
		if len(params) == 0 {
			return prune(ctx, s)
		}
	case "serve":
		if len(params) == 2 {
			return nil, serve(s, params[0], params[1])
		}
	case "monitor":
		if len(params) == 2 {
			return nil, monitor(s, params[0], params[1])
		}
	case "guard-ssh":
		if len(params) == 3 {
			return nil, guardSSH(s, params[0], params[1], params[2])
		}
	case "start", "ensure", "stop", "reset", "remove", "doctor":
		if cmd == "remove" && !group {
			break
		}
		if len(params) == 0 || !namePattern.MatchString(params[0]) {
			break
		}
		foreground := cmd == "start" && len(params) == 2 && params[1] == "--foreground"
		checked := cmd == "ensure" && len(params) == 2
		if !foreground && !checked && len(params) != 1 {
			break
		}
		name := params[0]
		switch cmd {
		case "start", "ensure":
			var id []string
			if checked {
				id = params[1:]
			}
			result, err := start(ctx, s, name, foreground, id...)
			if foreground {
				return nil, err
			}
			return result, err
		case "stop", "reset", "remove":
			p, err := stop(ctx, s, name)
			if err != nil || cmd == "stop" {
				return p, err
			}
			if cmd == "reset" {
				// Keep the configured profile and public port. Stop already reset the recovery budget.
				return p, nil
			}
			ok, err := s.remove(ctx, p)
			if err == nil && !ok {
				err = errOwnership
			}
			return p, err
		case "doctor":
			p, err := s.get(ctx, name)
			if err != nil {
				return nil, err
			}
			_, sshErr := exec.LookPath("ssh")
			return struct {
				status
				SSH bool   `json:"ssh_available"`
				Log string `json:"log"`
			}{describe(ctx, p), sshErr == nil, filepath.Join(dir, p.ID+".log")}, nil
		}
	}
	return nil, fmt.Errorf("invalid proxy command or arguments: %s", cmd)
}

func main() {
	dir := flag.String("state-dir", "", "CCM proxy state directory")
	flag.Parse()
	if args := flag.Args(); len(args) == 3 && args[0] == "guard-setup" && filepath.IsAbs(*dir) {
		if err := guardSetup(args[1], args[2]); err != nil {
			fmt.Fprintln(os.Stderr, err)
			if errors.Is(err, errSSHCleanup) {
				os.Exit(2)
			}
			os.Exit(1)
		}
		return
	}
	out := struct {
		Version int    `json:"version"`
		Result  any    `json:"result,omitempty"`
		Error   string `json:"error,omitempty"`
		Code    string `json:"code,omitempty"`
	}{Version: schemaVersion}
	var err error
	if !filepath.IsAbs(*dir) {
		err = errors.New("an absolute --state-dir is required")
	} else {
		out.Result, err = run(*dir, flag.Args())
	}
	if err != nil {
		out.Error = err.Error()
		if errors.Is(err, errSetupConflict) {
			out.Code = "setup_conflict"
		} else if errors.Is(err, errSetupProbe) {
			out.Code = "setup_probe"
		}
	}
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		os.Exit(1)
	}
	if out.Error != "" {
		if errors.Is(err, errSSHCleanup) {
			os.Exit(2)
		}
		os.Exit(1)
	}
}
