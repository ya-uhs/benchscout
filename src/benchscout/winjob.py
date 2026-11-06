"""Windows process ownership for an approved execution plan."""

import os
import subprocess
import sys
import time

if sys.platform == "win32":
    import _winapi
    import msvcrt

    import win32api
    import win32event
    import win32job
    import win32process

    class WindowsJob:
        def __init__(self, argv, cwd, stdout, stderr):
            self.job = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
            with open(os.devnull, "rb") as stdin:
                current = win32api.GetCurrentProcess()
                streams = [win32api.DuplicateHandle(current, msvcrt.get_osfhandle(f.fileno()), current,
                                                    0, True, 2) for f in (stdin, stdout, stderr)]
                handles = list(map(int, streams))
                startup = subprocess.STARTUPINFO()
                startup.dwFlags = subprocess.STARTF_USESTDHANDLES
                startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
                startup.lpAttributeList = {"handle_list": handles}
                # CREATE_SUSPENDED: assign the job before any user code can spawn children.
                process, thread, _, _ = _winapi.CreateProcess(
                    argv[0], subprocess.list2cmdline(argv), None, None, True, 4, dict(os.environ), cwd, startup)
                try:
                    win32job.AssignProcessToJobObject(self.job, process)
                    win32process.ResumeThread(thread)
                except BaseException:
                    _winapi.TerminateProcess(process, 1)
                    _winapi.CloseHandle(process)
                    raise
                finally:
                    _winapi.CloseHandle(thread)
            self.process = process

        def poll(self):
            if win32event.WaitForSingleObject(self.process, 0) == win32event.WAIT_OBJECT_0:
                return win32process.GetExitCodeProcess(self.process)
            return None

        def stop(self):
            win32job.TerminateJobObject(self.job, 1)
            deadline = time.monotonic() + 5
            while win32job.QueryInformationJobObject(self.job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Windows job still has active processes")
                time.sleep(0.01)
            code = self.poll()
            _winapi.CloseHandle(self.process)
            self.job.Close()
            return code
