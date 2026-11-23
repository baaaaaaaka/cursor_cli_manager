package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

var (
	errSetupConflict = errors.New("proxy settings changed during setup")
	errSetupProbe    = errors.New("SSH validation failed")
)

type proxyPreference struct {
	Revision int64  `json:"revision"`
	Mode     string `json:"mode"`
	ID       string `json:"id,omitempty"`
}

type setupSnapshot struct {
	proxyPreference
	Profiles []profile `json:"profiles"`
}

func (s *store) setupStatus(ctx context.Context) (setupSnapshot, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return setupSnapshot{}, err
	}
	defer tx.Rollback()
	state := setupSnapshot{proxyPreference: proxyPreference{Mode: "unset"}}
	var exists int
	if err := tx.QueryRowContext(ctx, "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name='preference'").Scan(&exists); err != nil {
		return state, err
	}
	if exists != 0 {
		var p proxyPreference
		err := tx.QueryRowContext(ctx, "SELECT revision, mode, profile_id FROM preference WHERE singleton = 1").
			Scan(&p.Revision, &p.Mode, &p.ID)
		if err == nil {
			state.proxyPreference = p
		} else if !errors.Is(err, sql.ErrNoRows) {
			return state, err
		}
	}
	state.Profiles, err = listProfiles(ctx, tx)
	return state, err
}

func (s *store) savePreference(ctx context.Context, revision int64, p profile, create bool) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS preference (
		singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
		revision INTEGER NOT NULL CHECK (revision >= 0),
		mode TEXT NOT NULL,
		profile_id TEXT NOT NULL,
		CHECK ((mode IN ('unset', 'direct') AND profile_id = '') OR (mode = 'ssh' AND length(profile_id) = 32))
	)`); err != nil {
		return err
	}
	// Claim the write transaction before comparing revisions. No prompt or
	// SSH process keeps a transaction open.
	if _, err := tx.ExecContext(ctx, "INSERT OR IGNORE INTO preference VALUES (1, 0, 'unset', '')"); err != nil {
		return err
	}
	mode := "direct"
	if p.ID != "" {
		mode = "ssh"
	}
	res, err := tx.ExecContext(ctx, `UPDATE preference SET revision = revision + 1, mode = ?, profile_id = ?
		WHERE singleton = 1 AND revision = ?`, mode, p.ID, revision)
	if err != nil {
		return err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return errSetupConflict
	}
	if create {
		if err := insertProfile(ctx, tx, p); err != nil {
			return err
		}
	} else if p.ID != "" {
		current, err := decode(tx.QueryRowContext(ctx, "SELECT rev, data FROM profiles WHERE name = ?", p.Name))
		if errors.Is(err, errMissing) || (err == nil && current.ID != p.ID) {
			return errors.New("selected proxy profile was removed; run ccm proxy init")
		}
		if err != nil {
			return err
		}
	}
	return tx.Commit()
}

func runSetup(ctx context.Context, dir string, args []string) (any, error) {
	if len(args) == 1 && args[0] == "status" {
		s, err := openStore(ctx, dir, false)
		if errors.Is(err, errMissing) {
			return setupSnapshot{proxyPreference: proxyPreference{Mode: "unset"}, Profiles: []profile{}}, nil
		}
		if err != nil {
			return nil, err
		}
		defer s.db.Close()
		return s.setupStatus(ctx)
	}
	ctx, cancel := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer cancel()
	if len(args) > 1 && args[0] == "inspect" {
		p, err := parseProfile(args[1:])
		if err != nil {
			return nil, fmt.Errorf("%w: %v", errSetupProbe, err)
		}
		return runSetupSSH(ctx, dir, p, "inspect")
	}
	if len(args) < 2 {
		return nil, errors.New("invalid proxy setup arguments")
	}
	revision, err := strconv.ParseInt(args[1], 10, 64)
	if err != nil || revision < 0 {
		return nil, errors.New("invalid proxy preference revision")
	}
	var p profile
	create := args[0] == "add"
	switch {
	case create:
		p, err = parseProfile(args[2:])
		if err != nil {
			return nil, fmt.Errorf("%w: %v", errSetupProbe, err)
		}
	case args[0] == "use" && len(args) == 4:
		s, err := openStore(ctx, dir, false)
		if err != nil {
			return nil, err
		}
		p, err = s.get(ctx, args[2])
		s.db.Close()
		if err != nil {
			return nil, err
		}
		if p.ID != args[3] {
			return nil, errors.New("selected proxy profile changed; run ccm proxy init")
		}
	case args[0] == "direct" && len(args) == 2:
	default:
		return nil, errors.New("invalid proxy setup arguments")
	}
	if p.ID != "" {
		if _, err := runSetupSSH(ctx, dir, p, "probe"); err != nil {
			return nil, err
		}
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	s, err := openStore(ctx, dir, true)
	if err != nil {
		return nil, err
	}
	defer s.db.Close()
	return nil, s.savePreference(ctx, revision, p, create)
}

// The same guardian lifetime pipe used by the running gateway also cleans up
// an unpublished candidate if its setup helper is interrupted or killed.
func runSetupSSH(ctx context.Context, dir string, p profile, mode string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 12*time.Second)
	defer cancel()
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	var addr string
	if mode == "probe" {
		addr, err = randomSOCKSAddress()
		if err != nil {
			return "", err
		}
	}
	payload, err := json.Marshal(p)
	if err != nil {
		return "", err
	}
	cmd := exec.Command(exe, "--state-dir", dir, "guard-setup", mode, addr)
	var output, diagnostic setupOutput
	cmd.Stdout, cmd.Stderr = &output, &diagnostic
	child, err := startGuardianProcess(cmd, append(payload, '\n'))
	if err != nil {
		return "", err
	}
	if mode == "probe" {
		err = waitForSSH(ctx, child, addr, p.Target)
	} else {
		select {
		case <-child.done:
			err = child.err
		case <-ctx.Done():
			err = ctx.Err()
		}
	}
	if cleanup := child.close(); cleanup != nil {
		return "", fmt.Errorf("%w: %v", errSSHCleanup, cleanup)
	}
	if ctx.Err() != nil {
		err = ctx.Err()
	}
	if err != nil {
		return "", setupSSHError(p, diagnostic.buf.String(), err)
	}
	return output.buf.String(), nil
}

type setupOutput struct{ buf bytes.Buffer }

func (b *setupOutput) Write(p []byte) (int, error) {
	remaining := 16384 - b.buf.Len()
	if remaining > 0 {
		b.buf.Write(p[:min(remaining, len(p))])
	}
	return len(p), nil
}

func setupSSHError(p profile, diagnostic string, err error) error {
	lower := strings.ToLower(diagnostic)
	var hint string
	switch {
	case strings.Contains(lower, "remote host identification has changed"):
		hint = "SSH host key changed. Verify the new fingerprint with the server administrator before changing known_hosts"
	case strings.Contains(lower, "no ") && strings.Contains(lower, "host key is known"):
		hint = "SSH host key is unknown. Verify its fingerprint and trust it with OpenSSH before retrying"
	case strings.Contains(lower, "host key verification failed"):
		hint = "SSH host key verification failed. Check the fingerprint and OpenSSH known_hosts settings"
	case strings.Contains(lower, "permission denied") || strings.Contains(lower, "too many authentication failures"):
		hint = "SSH authentication failed. Configure a working SSH key or agent for this host before retrying"
	case strings.Contains(lower, "forwarding failed") || strings.Contains(lower, "administratively prohibited"):
		hint = "SSH forwarding failed. Check the SSH server and jump host forwarding permissions"
	case errors.Is(err, context.DeadlineExceeded) || strings.Contains(lower, "timed out"):
		hint = "SSH validation timed out. Check connectivity and any configured jump host or route target"
	case strings.Contains(lower, "could not resolve hostname") || strings.Contains(lower, "connection refused") ||
		strings.Contains(lower, "no route to host") || strings.Contains(lower, "network is unreachable"):
		hint = "SSH host is unreachable. Check the hostname, port and jump host"
	case strings.Contains(lower, "executable file not found"):
		hint = "OpenSSH client was not found on PATH"
	default:
		hint = err.Error()
		if p.Target != "" {
			hint += " (configured route target " + p.Target + ")"
		}
	}
	if details := strings.TrimSpace(diagnostic); details != "" {
		hint += "\n" + details
	}
	return fmt.Errorf("%w for %s: %s", errSetupProbe, p.Host, hint)
}

func guardSetup(mode, addr string) error {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	input := json.NewDecoder(io.LimitReader(os.Stdin, 65536))
	var p profile
	if err := input.Decode(&p); err != nil {
		return err
	}
	go func() {
		io.Copy(io.Discard, os.Stdin)
		cancel()
	}()
	var ssh *process
	var cmd *exec.Cmd
	var err error
	switch mode {
	case "inspect":
		args := append([]string{"-G"}, sshTargetArgs(p)...)
		cmd = exec.Command("ssh", args...)
		cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
		ssh, err = startSSHProcess(cmd)
	case "probe":
		host, port, splitErr := net.SplitHostPort(addr)
		n, portErr := strconv.Atoi(port)
		if splitErr != nil || host != "127.0.0.1" || portErr != nil || n <= 0 || n > 65535 {
			return errors.New("invalid setup SOCKS listener")
		}
		var removeConfig func()
		ssh, removeConfig, err = runSSHWithOutput(p, addr, io.Discard, os.Stderr)
		if removeConfig != nil {
			defer removeConfig()
		}
	default:
		return errors.New("invalid SSH setup mode")
	}
	if err != nil {
		return err
	}
	select {
	case <-ctx.Done():
		err = ctx.Err()
	case <-ssh.done:
		if mode == "probe" {
			err = errors.New("SSH exited before validation completed")
		}
	}
	if cleanup := ssh.close(); cleanup != nil {
		return fmt.Errorf("%w: %v", errSSHCleanup, cleanup)
	}
	if cmd != nil && (cmd.ProcessState == nil || !cmd.ProcessState.Success()) && err == nil {
		return errors.New("OpenSSH could not resolve the selected host")
	}
	return err
}
