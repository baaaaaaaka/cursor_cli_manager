package main

import (
	"os/exec"
	"syscall"

	"golang.org/x/sys/unix"
)

func groupAttrs(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func observeExit(pid int) error {
	queue, err := unix.Kqueue()
	if err != nil {
		return err
	}
	defer unix.Close(queue)
	unix.CloseOnExec(queue)
	registration := unix.Kevent_t{
		Ident: uint64(pid), Filter: unix.EVFILT_PROC, Flags: unix.EV_ADD | unix.EV_ONESHOT, Fflags: unix.NOTE_EXIT,
	}
	events := make([]unix.Kevent_t, 1)
	changes := []unix.Kevent_t{registration}
	for {
		n, err := unix.Kevent(queue, changes, events, nil)
		if n != 0 {
			if events[0].Flags&unix.EV_ERROR != 0 && events[0].Data != 0 && syscall.Errno(events[0].Data) != unix.ESRCH {
				return syscall.Errno(events[0].Data)
			}
			return nil
		}
		if err == unix.ESRCH {
			return nil // The process exited before the observer registered.
		}
		if err != unix.EINTR {
			return err
		}
	}
}
