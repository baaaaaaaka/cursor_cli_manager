//go:build linux || darwin

package main

import "os"

func sshConfigTempRoot() string {
	if dir := os.TempDir(); jumpConfigPath.MatchString(dir) {
		return dir
	}
	return "/tmp"
}
