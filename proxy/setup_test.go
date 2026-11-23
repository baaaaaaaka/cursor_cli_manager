package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestSetupStatusDoesNotCreateOrMigrateState(t *testing.T) {
	ctx := context.Background()
	dir := filepath.Join(t.TempDir(), "state")
	result, err := run(dir, []string{"profile", "list", "--setup"})
	if err != nil || result.(setupSnapshot).Mode != "unset" {
		t.Fatal(result, err)
	}
	if _, err := os.Stat(dir); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("read-only setup status created state", err)
	}
	s, err := openStore(ctx, dir, true)
	if err != nil {
		t.Fatal(err)
	}
	p, err := parseProfile([]string{"fixture", "--host", "alias"})
	if err != nil {
		t.Fatal(err)
	}
	if err := s.add(ctx, p); err != nil {
		t.Fatal(err)
	}
	if err := s.db.Close(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "proxy-v1.sqlite3")
	before, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	result, err = run(dir, []string{"profile", "list", "--setup"})
	if err != nil {
		t.Fatal(err)
	}
	state := result.(setupSnapshot)
	if state.Mode != "unset" || len(state.Profiles) != 1 || state.Profiles[0].ID != p.ID {
		t.Fatal(state)
	}
	after, err := os.ReadFile(path)
	if err != nil || !reflect.DeepEqual(before, after) {
		t.Fatal("read-only setup status modified a legacy database", err)
	}
}

func TestSetupSubmissionUsesOneTransaction(t *testing.T) {
	ctx := context.Background()
	s, err := openStore(ctx, t.TempDir(), true)
	if err != nil {
		t.Fatal(err)
	}
	defer s.db.Close()
	p, _ := parseProfile([]string{"fixture", "--host", "alias"})
	other, _ := parseProfile([]string{"other", "--host", "other-alias"})
	if _, err := s.db.ExecContext(ctx, `CREATE TRIGGER fail_add BEFORE INSERT ON profiles
		BEGIN SELECT RAISE(FAIL, 'injected profile failure'); END`); err != nil {
		t.Fatal(err)
	}
	if err := s.savePreference(ctx, 0, p, true); err == nil {
		t.Fatal("expected setup transaction failure")
	}
	state, err := s.setupStatus(ctx)
	if err != nil || state.Mode != "unset" || state.Revision != 0 || len(state.Profiles) != 0 {
		t.Fatal(state, err)
	}
	if _, err := s.db.ExecContext(ctx, "DROP TRIGGER fail_add"); err != nil {
		t.Fatal(err)
	}

	second, err := openStore(ctx, s.dir, true)
	if err != nil {
		t.Fatal(err)
	}
	defer second.db.Close()
	results := make(chan error, 2)
	go func() { results <- s.savePreference(ctx, 0, p, true) }()
	go func() { results <- second.savePreference(ctx, 0, other, true) }()
	a, b := <-results, <-results
	if !((a == nil && errors.Is(b, errSetupConflict)) || (b == nil && errors.Is(a, errSetupConflict))) {
		t.Fatal(a, b)
	}
	state, err = s.setupStatus(ctx)
	if err != nil || state.Mode != "ssh" || state.Revision != 1 || len(state.Profiles) != 1 ||
		state.Profiles[0].ID != state.ID {
		t.Fatal(state, err)
	}
	winner := state.Profiles[0]
	if ok, err := s.remove(ctx, winner); !ok || err != nil {
		t.Fatal(ok, err)
	}
	replacement, _ := parseProfile([]string{winner.Name, "--host", "replacement"})
	if err := s.add(ctx, replacement); err != nil {
		t.Fatal(err)
	}
	if _, err := start(ctx, s, winner.Name, false, winner.ID); err == nil || !strings.Contains(err.Error(), "profile changed") {
		t.Fatal("a stale selection could run a different host", err)
	}
	if err := s.savePreference(ctx, 1, winner, false); err == nil {
		t.Fatal("saved preference accepted a stale profile")
	}
	state, err = s.setupStatus(ctx)
	if err != nil || state.Revision != 1 || state.ID != winner.ID || state.Profiles[0].ID == winner.ID {
		t.Fatal(state, err)
	}
	if err := s.savePreference(ctx, 1, profile{}, false); err != nil {
		t.Fatal(err)
	}
	state, err = s.setupStatus(ctx)
	if err != nil || state.Mode != "direct" || state.ID != "" || state.Revision != 2 || len(state.Profiles) != 1 {
		t.Fatal(state, err)
	}
}

func TestConfigBasedProfileKeepsOpenSSHArguments(t *testing.T) {
	config := filepath.Join(t.TempDir(), "SSH config with spaces")
	p, err := parseProfile([]string{"saved", "--host", "jump-alias", "--ssh-config", config})
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"-F", config, "--", "jump-alias"}
	if args := sshTargetArgs(p); !reflect.DeepEqual(want, args) {
		t.Fatal(args)
	}
	if _, err := parseProfile([]string{"saved", "--host", "jump-alias", "--ssh-config", config, "--port", "2201"}); err == nil {
		t.Fatal("accepted an override to a config-based profile")
	}
}

func TestSetupStagesShellSensitiveSSHConfig(t *testing.T) {
	path := filepath.Join(t.TempDir(), "SSH config % settings")
	body := "Host demo\n ProxyJump hop\n"
	if err := os.WriteFile(path, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	staged, cleanup, err := prepareSSHConfig(profile{SSHConfig: path})
	if err != nil {
		t.Fatal(err)
	}
	defer cleanup()
	if staged.SSHConfig == path || !jumpConfigPath.MatchString(staged.SSHConfig) {
		t.Fatal(staged.SSHConfig)
	}
	if data, err := os.ReadFile(staged.SSHConfig); err != nil || string(data) != body {
		t.Fatal(string(data), err)
	}
	cleanup()
	if _, err := os.Stat(staged.SSHConfig); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("staged config was not removed", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal("original SSH config was removed", err)
	}
}

func TestSetupDiagnosticsPreserveHostKeyFailureClasses(t *testing.T) {
	for _, test := range []struct {
		log, want string
	}{
		{"WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\nHost key verification failed.", "host key changed"},
		{"No ED25519 host key is known for example\nHost key verification failed.", "host key is unknown"},
		{"Permission denied (publickey).", "authentication failed"},
		{"Could not resolve hostname fixture", "host is unreachable"},
		{"channel 2: open failed: administratively prohibited", "forwarding failed"},
	} {
		err := setupSSHError(profile{Host: "fixture"}, test.log, errors.New("ssh exited"))
		if !errors.Is(err, errSetupProbe) || !strings.Contains(err.Error(), test.want) {
			t.Fatal(err)
		}
	}
}
