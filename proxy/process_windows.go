package main

import (
	"errors"
	"os/exec"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

func detach(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NEW_PROCESS_GROUP | windows.DETACHED_PROCESS}
}

type process struct {
	job  windows.Handle
	done chan struct{}
}

func startSSHProcess(cmd *exec.Cmd) (*process, error) {
	// A frozen CCM parent can set a bundle DLL directory. SSH uses its
	// normal system library search path instead of CCM's bundled DLLs.
	setDLLDirectory := windows.NewLazySystemDLL("kernel32.dll").NewProc("SetDllDirectoryW")
	if result, _, err := setDLLDirectory.Call(0); result == 0 {
		return nil, err
	}
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return nil, err
	}
	limits := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	limits.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(job, windows.JobObjectExtendedLimitInformation, uintptr(unsafe.Pointer(&limits)), uint32(unsafe.Sizeof(limits))); err != nil {
		windows.CloseHandle(job)
		return nil, err
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NO_WINDOW | windows.CREATE_SUSPENDED}
	if err := cmd.Start(); err != nil {
		windows.CloseHandle(job)
		return nil, err
	}
	p := &process{job: job, done: make(chan struct{})}
	fail := func(err error) (*process, error) {
		windows.CloseHandle(job)
		cmd.Process.Kill()
		cmd.Wait()
		return nil, err
	}
	handle, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(cmd.Process.Pid))
	if err != nil {
		return fail(err)
	}
	err = windows.AssignProcessToJobObject(job, handle)
	windows.CloseHandle(handle)
	if err != nil {
		return fail(err)
	}

	// os/exec closes the primary thread handle before returning. Find the
	// suspended thread and resume it only after assignment to the job.
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPTHREAD, 0)
	if err != nil {
		return fail(err)
	}
	defer windows.CloseHandle(snapshot)
	entry := windows.ThreadEntry32{}
	entry.Size = uint32(unsafe.Sizeof(entry))
	for err = windows.Thread32First(snapshot, &entry); err == nil; err = windows.Thread32Next(snapshot, &entry) {
		if entry.OwnerProcessID != uint32(cmd.Process.Pid) {
			continue
		}
		thread, err := windows.OpenThread(windows.THREAD_SUSPEND_RESUME, false, entry.ThreadID)
		if err != nil {
			return fail(err)
		}
		_, err = windows.ResumeThread(thread)
		windows.CloseHandle(thread)
		if err != nil {
			return fail(err)
		}
		go func() { cmd.Wait(); close(p.done) }()
		return p, nil
	}
	return fail(errors.New("could not find suspended SSH thread"))
}

func (p *process) close() error {
	defer windows.CloseHandle(p.job)
	if err := windows.TerminateJobObject(p.job, 1); err != nil {
		return err
	}
	var accounting struct {
		userTime, kernelTime, intervalUserTime, intervalKernelTime       int64
		pageFaults, totalProcesses, activeProcesses, terminatedProcesses uint32
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		err := windows.QueryInformationJobObject(p.job, windows.JobObjectBasicAccountingInformation,
			uintptr(unsafe.Pointer(&accounting)), uint32(unsafe.Sizeof(accounting)), nil)
		if err != nil {
			return err
		}
		if accounting.activeProcesses == 0 {
			select {
			case <-p.done:
				return nil
			case <-time.After(time.Until(deadline)):
				break
			}
		}
		time.Sleep(25 * time.Millisecond)
	}
	return errors.New("SSH job still has active processes")
}
