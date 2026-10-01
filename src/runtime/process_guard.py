"""Run a task with a deadline that also covers native code and its children."""

import faulthandler
import multiprocessing
import os
import signal


def _worker_entry(target, args):
    # ffmpeg inherits this process group, so a timeout also stops downloads.
    if os.name == "posix":
        os.setsid()
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    target(*args)


def _signal_worker(worker, sig):
    if os.name == "posix":
        try:
            os.killpg(worker.pid, sig)
            return
        except ProcessLookupError:
            pass  # The worker may not have called setsid yet.
    if worker.is_alive():
        if sig == signal.SIGTERM:
            worker.terminate()
        elif sig == signal.SIGKILL:
            worker.kill()


def run_with_timeout(target, args, timeout: float) -> int:
    """Return the exit code; raise TimeoutError after stopping a stuck worker.

    Spawn avoids inheriting parent SQLite connections and thread locks.
    Results are committed to the database by the worker rather than passed
    through a pipe, which could itself block on a large lecture summary.
    """
    if timeout <= 0:
        raise ValueError("worker timeout must be positive")
    worker = multiprocessing.get_context("spawn").Process(
        target=_worker_entry, args=(target, args),
    )
    worker.start()
    try:
        worker.join(timeout)
        if worker.is_alive():
            if os.name == "posix":
                _signal_worker(worker, signal.SIGUSR1)
                worker.join(0.2)  # Allow the stack dump to reach the log.
            raise TimeoutError(f"Lecture exceeded {timeout:g} seconds")
        return worker.exitcode
    finally:
        # Also remove orphan ffmpeg processes after an unexpected worker exit.
        _signal_worker(worker, signal.SIGTERM)
        worker.join(5)
        _signal_worker(worker, signal.SIGKILL)
        worker.join()
        worker.close()
