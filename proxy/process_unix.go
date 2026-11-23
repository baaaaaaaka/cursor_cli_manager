//go:build linux || darwin

package main

import (
	"errors"
	"os/exec"
	"syscall"
	"time"
)

func detach(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
}

type process struct {
	cmd        *exec.Cmd
	done       chan struct{}
	observeErr error
}

func startSSHProcess(cmd *exec.Cmd) (*process, error) {
	groupAttrs(cmd)
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	p := &process{cmd: cmd, done: make(chan struct{})}
	go func() {
		// Keep the exited leader unreaped until group signaling is complete.
		// Another process cannot reuse its identity during cleanup.
		p.observeErr = observeExit(cmd.Process.Pid)
		close(p.done)
	}()
	return p, nil
}

func (p *process) close() error {
	processGroup := -p.cmd.Process.Pid
	if err := syscall.Kill(processGroup, syscall.SIGTERM); err != nil && !errors.Is(err, syscall.ESRCH) {
		return err
	}
	select {
	case <-p.done:
	case <-time.After(750 * time.Millisecond):
	}
	if err := syscall.Kill(processGroup, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
		return err
	}
	select {
	case <-p.done:
		if p.observeErr != nil {
			// Still reap the process when the native observer could not run.
			go p.cmd.Wait()
			return p.observeErr
		}
	case <-time.After(2 * time.Second):
		return errors.New("SSH process did not exit")
	}
	p.cmd.Wait() // A signaled SSH process normally reports a nonzero exit status.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if err := syscall.Kill(processGroup, 0); errors.Is(err, syscall.ESRCH) {
			return nil
		}
		time.Sleep(25 * time.Millisecond)
	}
	return errors.New("SSH process group did not disappear")
}
