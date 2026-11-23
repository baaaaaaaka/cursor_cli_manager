package main

import (
	"os/exec"
	"syscall"

	"golang.org/x/sys/unix"
)

func groupAttrs(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pdeathsig: syscall.SIGKILL}
}

func signalSSHGroup(pgid int, signal syscall.Signal) error {
	return syscall.Kill(-pgid, signal)
}

func observeExit(pid int) error {
	for {
		var info unix.Siginfo
		err := unix.Waitid(unix.P_PID, pid, &info, unix.WEXITED|unix.WNOWAIT, nil)
		if err != unix.EINTR {
			return err
		}
	}
}
