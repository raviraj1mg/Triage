"""Finding and stopping a running server.

Two ways to find it, because either alone is wrong. The pid file misses a
server someone started from their own terminal after the file was cleaned up,
or points at a pid that has since died and been reused. The port tells you what
is listening but not what it is -- and on this machine 8080 is nginx, so
"whatever holds the port" is not something to send a signal to.

So: read the pid file, fall back to the port, and in both cases confirm the
process actually is an oncallbot before signalling it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Server:
    pid: int
    port: int
    command: str
    source: str   # "pidfile" or "port"


def pid_path(store_path: Path) -> Path:
    """Next to the store, so it lands in .data/ -- gitignored, never shipped."""
    return store_path.parent / "oncallbot.pid"


def write_pidfile(path: Path, port: int, pid: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid or os.getpid()} {port}\n")


def clear_pidfile(path: Path, pid: int | None = None) -> None:
    """Remove it, but only if it is still ours."""
    recorded = read_pidfile(path)
    if recorded is None:
        return
    if pid is not None and recorded[0] != pid:
        return
    path.unlink(missing_ok=True)


def read_pidfile(path: Path) -> tuple[int, int] | None:
    try:
        parts = path.read_text().split()
        return int(parts[0]), int(parts[1])
    except (OSError, ValueError, IndexError):
        return None


def process_command(pid: int) -> str:
    """The process's command line, or "" if there is no such process."""
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def is_oncallbot(command: str) -> bool:
    """Does this command line belong to a server of ours?

    The guard that matters: `stop` must never signal a process that merely
    happens to hold the port. Killing the machine's nginx because it answered
    on 8080 would be a far worse bug than failing to stop anything.
    """
    if not command:
        return False
    lowered = command.lower()
    if "oncallbot" not in lowered:
        return False
    # `restart` counts: the server it starts runs in that process, so its
    # command line says "restart" and `stop` must still be able to find it.
    # `stop`, `doctor` and the rest deliberately do not match.
    return any(word in lowered for word in ("serve", "restart", "uvicorn"))


def pid_on_port(port: int) -> int | None:
    try:
        out = subprocess.run(
            ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.split():
        try:
            return int(line)
        except ValueError:
            continue
    return None


def find_server(pidfile: Path, port: int) -> Server | None:
    """The running server, from the pid file or from the port. None if neither."""
    recorded = read_pidfile(pidfile)
    if recorded is not None:
        pid, recorded_port = recorded
        # Only if it is the port being asked about. The file lives beside the
        # store, so two configs sharing a store share the file -- and matching
        # on pid alone made `serve --port 8082` refuse because a server was
        # running on 8765.
        if recorded_port == port:
            command = process_command(pid)
            if is_oncallbot(command):
                return Server(
                    pid=pid, port=recorded_port, command=command, source="pidfile"
                )

    pid = pid_on_port(port)
    if pid is not None:
        command = process_command(pid)
        if is_oncallbot(command):
            return Server(pid=pid, port=port, command=command, source="port")
    return None


def port_holder(port: int) -> tuple[int, str] | None:
    """Whatever is on the port, oncallbot or not. For explaining a refusal."""
    pid = pid_on_port(port)
    if pid is None:
        return None
    return pid, process_command(pid)


def stop(server: Server, *, timeout: float = 10.0, force: bool = False) -> str:
    """Signal it and wait. Returns how it ended: "stopped", "killed", "stuck"."""
    os.kill(server.pid, signal.SIGKILL if force else signal.SIGTERM)
    if force:
        return "killed"

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not process_command(server.pid):
            return "stopped"
        time.sleep(0.2)

    # It ignored SIGTERM. Escalating beats leaving a port held by a process
    # nobody can see.
    try:
        os.kill(server.pid, signal.SIGKILL)
    except ProcessLookupError:
        return "stopped"
    for _ in range(25):
        if not process_command(server.pid):
            return "killed"
        time.sleep(0.2)
    return "stuck"
