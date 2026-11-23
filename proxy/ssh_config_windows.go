package main

import (
	"os"
	"path/filepath"

	"golang.org/x/sys/windows"
)

func sshConfigTempRoot() string {
	dir := os.TempDir()
	if jumpConfigPath.MatchString(filepath.ToSlash(dir)) {
		return dir
	}
	windowsPath, err := windows.UTF16PtrFromString(dir)
	if err != nil {
		return dir
	}
	var short [windows.MAX_PATH]uint16
	n, err := windows.GetShortPathName(windowsPath, &short[0], uint32(len(short)))
	if err != nil || n == 0 || int(n) >= len(short) {
		return dir
	}
	return windows.UTF16ToString(short[:n])
}
