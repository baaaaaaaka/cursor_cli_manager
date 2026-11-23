package main

import (
	"errors"
	"fmt"
	"os/exec"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

func groupAttrs(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func observeExit(pid int) error {
	err := waitForExitEvent(pid)
	if !errors.Is(err, unix.EPERM) {
		return err
	}
	// Some macOS environments reject the process filter. Look up the
	// child's status without reaping it or releasing its process group ID.
	const zombie = 5 // Darwin's SZOMB from sys/proc.h.
	for {
		entries, err := unix.SysctlKinfoProcSlice("kern.proc.pid", pid)
		if err != nil {
			return fmt.Errorf("read SSH child status: %w", err)
		}
		if len(entries) == 0 || entries[0].Proc.P_stat == zombie {
			return nil
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func waitForExitEvent(pid int) error {
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
		if n > 0 {
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
