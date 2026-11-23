package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
)

var jumpConfigPath = regexp.MustCompile(`^[a-zA-Z0-9_./:~-]+$`)

// OpenSSH inserts -F verbatim into the shell command it builds for
// ProxyJump. Keep the original selection in state and expose the same
// configuration to OpenSSH via a shell-safe path for this process only.
func prepareSSHConfig(p profile) (profile, func(), error) {
	noop := func() {}
	if p.SSHConfig == "" {
		return p, noop, nil
	}
	if path := filepath.ToSlash(p.SSHConfig); jumpConfigPath.MatchString(path) {
		p.SSHConfig = path
		return p, noop, nil
	}
	root := sshConfigTempRoot()
	if !jumpConfigPath.MatchString(filepath.ToSlash(root)) || !filepath.IsAbs(root) {
		return p, nil, errors.New("OpenSSH ProxyJump needs a shell-safe temporary directory for this config path")
	}
	dir, err := os.MkdirTemp(root, "ccm-ssh-")
	if err != nil {
		return p, nil, fmt.Errorf("stage OpenSSH config: %w", err)
	}
	cleanup := func() { os.RemoveAll(dir) }
	path := filepath.Join(dir, "config")
	if runtime.GOOS == "windows" {
		// Windows commonly requires special privileges to create symlinks.
		err = copySSHConfig(p.SSHConfig, path)
	} else {
		err = os.Symlink(p.SSHConfig, path)
	}
	if err != nil {
		cleanup()
		return p, nil, fmt.Errorf("stage OpenSSH config: %w", err)
	}
	p.SSHConfig = filepath.ToSlash(path)
	return p, cleanup, nil
}

func copySSHConfig(source, destination string) error {
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(destination, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	_, err = io.Copy(out, in)
	return errors.Join(err, out.Close())
}
