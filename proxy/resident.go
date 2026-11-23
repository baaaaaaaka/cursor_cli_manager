package main

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
)

// A monitor and its future children outlive an installed wheel or release.
// Run them from a private immutable copy, named for the contents of the helper.
func residentHelper(stateDir string) (string, error) {
	executable, err := os.Executable()
	if err != nil {
		return "", err
	}
	source, err := os.Open(executable)
	if err != nil {
		return "", err
	}
	defer source.Close()
	root := filepath.Join(stateDir, "workers")
	if err := os.MkdirAll(root, 0700); err != nil {
		return "", err
	}
	staging, err := os.MkdirTemp(root, ".worker-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(staging)

	name := "ccm-proxy"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	destination, err := os.OpenFile(filepath.Join(staging, name), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0700)
	if err != nil {
		return "", err
	}
	hash := sha256.New()
	_, copyErr := io.Copy(io.MultiWriter(destination, hash), source)
	if err := errors.Join(copyErr, destination.Close()); err != nil {
		return "", err
	}

	version := filepath.Join(root, hex.EncodeToString(hash.Sum(nil)))
	resident := filepath.Join(version, name)
	if err := os.Rename(staging, version); err != nil {
		// Another start may have installed the same immutable worker first.
		if info, statErr := os.Stat(resident); statErr != nil || !info.Mode().IsRegular() {
			return "", err
		}
	}
	return resident, nil
}
