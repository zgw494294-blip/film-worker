"""Process trees for Blender, FFmpeg and Codex."""
import os
import signal
import subprocess


class ProcessTree:
    def __init__(self, command, *, cwd, env, log):
        self.job = None
        self.child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=log,
                                      stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
        try:
            if os.name == "nt":
                import win32api
                import win32job
                self.job = win32job.CreateJobObject(None, None)
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
        elif self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
                self.child.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(self.child.pid, signal.SIGKILL)
        self.child.wait(timeout=10)
