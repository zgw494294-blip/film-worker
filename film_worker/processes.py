"""Process trees for Blender, FFmpeg and Codex."""
import os
import signal
import subprocess


class ProcessTree:
    def __init__(self, command, *, cwd, env, log):
        self.job = None
        self.child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=log,
                                      stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            if os.name == "nt":
                import win32api
                import win32job
                self.job = win32job.CreateJobObject(None, "")
                limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
                limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
                process = win32api.OpenProcess(0x0101, False, self.child.pid)
                try:
                    win32job.AssignProcessToJobObject(self.job, process)
                finally:
                    process.Close()
            # The child waits for this byte before starting any subprocess.
            self.child.stdin.write(b"1")
            self.child.stdin.flush()
        except BaseException:
            self.child.kill()
            self.child.wait()
            if self.job is not None:
                self.job.Close()
            raise

    def close(self):
        self.child.stdin.close()
        if self.job is not None:
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
