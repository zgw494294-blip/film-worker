"""Process trees for Blender, FFmpeg and Codex."""
import os
import signal
import subprocess


def resume_windows_process(pid):
    """Resume the primary thread of a process created with CREATE_SUSPENDED."""
    import ctypes
    from ctypes import wintypes

    import win32api
    import win32con
    import win32process

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
            ("thread_id", wintypes.DWORD), ("owner_pid", wintypes.DWORD),
            ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ThreadEntry(size=ctypes.sizeof(ThreadEntry))
        for read in (kernel.Thread32First, kernel.Thread32Next):
            read.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
            read.restype = wintypes.BOOL
        found = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.owner_pid == pid:
                thread = win32api.OpenThread(win32con.THREAD_SUSPEND_RESUME, False, entry.thread_id)
                try:
                    win32process.ResumeThread(thread)
                finally:
                    thread.Close()
                return
            entry.size = ctypes.sizeof(ThreadEntry)
            found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        raise RuntimeError("Windows 工单进程没有可恢复的主线程")
    finally:
        win32api.CloseHandle(snapshot)


class ProcessTree:
    def __init__(self, command, *, cwd, env, log):
        self.job = None
        creation_flags = 0
        if os.name == "nt":
            import win32api
            import win32con
            import win32job

            # Windows venv launchers can start their interpreter before Python code reads stdin.
            creation_flags = subprocess.CREATE_NO_WINDOW | win32con.CREATE_SUSPENDED
        self.child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=log,
                                      stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
                                      creationflags=creation_flags)
        try:
            if os.name == "nt":
                self.job = win32job.CreateJobObject(None, "")
                limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
                limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
                process = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                                               False, self.child.pid)
                try:
                    win32job.AssignProcessToJobObject(self.job, process)
                finally:
                    process.Close()
                resume_windows_process(self.child.pid)
            # The child waits for this byte before starting any subprocess.
            self.child.stdin.write(b"1")
            self.child.stdin.flush()
        except BaseException:
            try:
                self.child.kill()
                self.child.wait()
            finally:
                self.child.stdin.close()
                if self.job is not None:
                    self.job.Close()
            raise

    def close(self):
        self.child.stdin.close()
        if self.job is not None:
            import win32job

            try:
                win32job.TerminateJobObject(self.job, 1)
            finally:
                self.job.Close()
        else:
            # The leader may have exited while its tool processes still hold the group.
            self.signal_group(signal.SIGTERM)
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.signal_group(signal.SIGKILL)
        self.child.wait(timeout=10)

    def signal_group(self, sig):
        try:
            os.killpg(self.child.pid, sig)
        except ProcessLookupError:
            pass
