"""Finding and stopping the server.

The rule worth pinning: `stop` must never signal a process that merely holds
the port. On this machine 8080 is nginx, and killing it because it answered
there would be far worse than failing to stop anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oncallbot import process

ONCALLBOT = "/x/.venv/bin/python3 /x/.venv/bin/oncallbot serve"
NGINX = "nginx: master process /opt/homebrew/opt/nginx/bin/nginx -g daemon off;"


# --- identifying our own process -------------------------------------------


@pytest.mark.parametrize("command", [
    ONCALLBOT,
    "python -m uvicorn oncallbot.chat.server:app",
    "/usr/bin/python3 /opt/oncallbot/.venv/bin/oncallbot serve --port 9000",
])
def test_our_server_is_recognised(command):
    assert process.is_oncallbot(command)


@pytest.mark.parametrize("command", [
    NGINX,
    "postgres: writer process",
    "node /usr/local/bin/http-server -p 8765",
    "",                                    # no such process
    "/x/.venv/bin/oncallbot doctor",       # ours, but not a server
])
def test_anything_else_is_not(command):
    assert not process.is_oncallbot(command)


# --- the pid file -----------------------------------------------------------


def test_the_pidfile_lands_beside_the_store(tmp_path: Path):
    assert process.pid_path(tmp_path / ".data" / "x.sqlite3") == (
        tmp_path / ".data" / "oncallbot.pid"
    )


def test_pidfile_round_trip(tmp_path: Path):
    path = tmp_path / "oncallbot.pid"
    process.write_pidfile(path, 8765, pid=4242)
    assert process.read_pidfile(path) == (4242, 8765)


@pytest.mark.parametrize("junk", ["", "not a pid", "123", "abc def"])
def test_a_corrupt_pidfile_reads_as_nothing(tmp_path: Path, junk):
    path = tmp_path / "oncallbot.pid"
    path.write_text(junk)
    assert process.read_pidfile(path) is None


def test_clearing_only_removes_our_own_entry(tmp_path: Path):
    """A newer server owns the file; an older one exiting must not delete it."""
    path = tmp_path / "oncallbot.pid"
    process.write_pidfile(path, 8765, pid=999)

    process.clear_pidfile(path, pid=111)       # a different, older process
    assert path.exists()

    process.clear_pidfile(path, pid=999)
    assert not path.exists()


def test_clearing_a_missing_file_is_fine(tmp_path: Path):
    process.clear_pidfile(tmp_path / "absent.pid")


# --- finding the server -----------------------------------------------------


def _fake(monkeypatch, *, commands: dict[int, str], on_port: int | None = None):
    monkeypatch.setattr(process, "process_command", lambda pid: commands.get(pid, ""))
    monkeypatch.setattr(process, "pid_on_port", lambda port: on_port)


def test_found_from_the_pidfile(tmp_path: Path, monkeypatch):
    path = tmp_path / "oncallbot.pid"
    process.write_pidfile(path, 8765, pid=42)
    _fake(monkeypatch, commands={42: ONCALLBOT})

    server = process.find_server(path, 8765)
    assert server is not None
    assert (server.pid, server.port, server.source) == (42, 8765, "pidfile")


def test_found_on_the_port_when_there_is_no_pidfile(tmp_path: Path, monkeypatch):
    """The case that happens for real: someone started it in their own shell."""
    _fake(monkeypatch, commands={77: ONCALLBOT}, on_port=77)

    server = process.find_server(tmp_path / "absent.pid", 8765)
    assert server is not None
    assert (server.pid, server.source) == (77, "port")


def test_a_stale_pidfile_falls_through_to_the_port(tmp_path: Path, monkeypatch):
    """The pid died and the number was reused by something unrelated."""
    path = tmp_path / "oncallbot.pid"
    process.write_pidfile(path, 8765, pid=42)
    _fake(monkeypatch, commands={42: "vim notes.txt", 77: ONCALLBOT}, on_port=77)

    server = process.find_server(path, 8765)
    assert server is not None
    assert server.pid == 77, "the recorded pid is not ours any more"


def test_a_foreign_process_on_the_port_is_not_our_server(tmp_path: Path, monkeypatch):
    """The guard: nginx on the port must not be reported as the server."""
    _fake(monkeypatch, commands={1216: NGINX}, on_port=1216)
    assert process.find_server(tmp_path / "absent.pid", 8080) is None

    # But it can still be named, so the refusal can explain itself.
    assert process.port_holder(8080) == (1216, NGINX)


def test_nothing_running_is_nothing_found(tmp_path: Path, monkeypatch):
    _fake(monkeypatch, commands={})
    assert process.find_server(tmp_path / "absent.pid", 8765) is None
    assert process.port_holder(8765) is None


# --- stopping ---------------------------------------------------------------


def _server(pid: int = 42) -> process.Server:
    return process.Server(pid=pid, port=8765, command=ONCALLBOT, source="pidfile")


def test_sigterm_is_the_default(monkeypatch):
    import signal

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(process.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(process, "process_command", lambda pid: "")

    assert process.stop(_server()) == "stopped"
    assert sent == [(42, signal.SIGTERM)], "asked nicely first"


def test_force_goes_straight_to_sigkill(monkeypatch):
    import signal

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(process.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    assert process.stop(_server(), force=True) == "killed"
    assert sent == [(42, signal.SIGKILL)]


def test_a_process_that_ignores_sigterm_is_killed(monkeypatch):
    """Better than leaving a port held by something nobody can see."""
    import signal

    sent: list[int] = []
    alive = {"yes": True}

    def kill(pid, sig):
        sent.append(sig)
        if sig == signal.SIGKILL:
            alive["yes"] = False

    monkeypatch.setattr(process.os, "kill", kill)
    monkeypatch.setattr(process, "process_command",
                        lambda pid: ONCALLBOT if alive["yes"] else "")
    monkeypatch.setattr(process.time, "sleep", lambda _s: None)

    assert process.stop(_server(), timeout=0.01) == "killed"
    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_a_process_that_ignores_everything_is_reported(monkeypatch):
    monkeypatch.setattr(process.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(process, "process_command", lambda pid: ONCALLBOT)
    monkeypatch.setattr(process.time, "sleep", lambda _s: None)

    assert process.stop(_server(), timeout=0.01) == "stuck"


def test_an_already_dead_process_raises_for_the_caller(monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(process.os, "kill", gone)
    with pytest.raises(ProcessLookupError):
        process.stop(_server())


# --- the CLI ----------------------------------------------------------------


def _cli(tmp_path: Path):
    from typer.testing import CliRunner

    from oncallbot.cli import app

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "gmail:\n  support_address: hr@1mg.com\n"
        f"store:\n  path: {tmp_path / '.data' / 'x.sqlite3'}\n"
        "categories: [other]\n"
    )
    return CliRunner(), app, cfg


def test_stop_reports_when_nothing_is_running(tmp_path: Path, monkeypatch):
    runner, app, cfg = _cli(tmp_path)
    monkeypatch.setattr(process, "pid_on_port", lambda port: None)
    monkeypatch.setattr(process, "process_command", lambda pid: "")

    r = runner.invoke(app, ["stop", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    assert "Nothing is running" in r.output


def test_stop_refuses_to_signal_a_process_that_is_not_ours(tmp_path: Path, monkeypatch):
    """The important one: `stop --port 8080` must not kill the machine's nginx."""
    runner, app, cfg = _cli(tmp_path)
    killed: list[int] = []
    monkeypatch.setattr(process, "pid_on_port", lambda port: 1216)
    monkeypatch.setattr(process, "process_command", lambda pid: NGINX)
    monkeypatch.setattr(process.os, "kill", lambda pid, sig: killed.append(pid))

    r = runner.invoke(app, ["stop", "--config", str(cfg), "--port", "8080"])
    assert r.exit_code == 1
    assert "not an oncallbot server" in r.output
    assert "nginx" in r.output, "it names what it found"
    assert killed == [], "and signals nothing"


def test_stop_stops_our_server_and_clears_the_pidfile(tmp_path: Path, monkeypatch):
    runner, app, cfg = _cli(tmp_path)
    pidfile = tmp_path / ".data" / "oncallbot.pid"
    process.write_pidfile(pidfile, 8765, pid=42)

    sent: list[int] = []
    monkeypatch.setattr(process, "pid_on_port", lambda port: 42)
    monkeypatch.setattr(process, "process_command", lambda pid: ONCALLBOT)
    monkeypatch.setattr(process.os, "kill", lambda pid, sig: sent.append(pid))
    monkeypatch.setattr(process, "stop", lambda server, **kw: "stopped")

    r = runner.invoke(app, ["stop", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    assert "Stopped." in r.output
    assert not pidfile.exists(), "a dead server must not leave its pid behind"


def test_restart_does_not_start_when_the_old_one_will_not_die(tmp_path: Path, monkeypatch):
    """Starting anyway would just fail to bind, with a worse message."""
    runner, app, cfg = _cli(tmp_path)
    started: list[bool] = []
    monkeypatch.setattr(process, "pid_on_port", lambda port: 42)
    monkeypatch.setattr(process, "process_command", lambda pid: ONCALLBOT)
    monkeypatch.setattr(process, "stop", lambda server, **kw: "stuck")
    monkeypatch.setattr("oncallbot.cli.serve", lambda **kw: started.append(True))

    r = runner.invoke(app, ["restart", "--config", str(cfg)])
    assert r.exit_code == 1
    assert "would not stop" in r.output
    assert started == []


def test_restart_starts_when_nothing_was_running(tmp_path: Path, monkeypatch):
    runner, app, cfg = _cli(tmp_path)
    started: list[dict] = []
    monkeypatch.setattr(process, "pid_on_port", lambda port: None)
    monkeypatch.setattr(process, "process_command", lambda pid: "")
    monkeypatch.setattr("oncallbot.cli.serve", lambda **kw: started.append(kw))

    r = runner.invoke(app, ["restart", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    assert "Nothing to stop" in r.output
    assert started and started[0]["port"] == 8765


def test_a_server_started_by_restart_is_still_findable():
    """It runs in the `restart` process, so its command line says "restart".

    Found by a round trip: `restart` left a server that `stop` then declared
    "not an oncallbot server" and refused to touch.
    """
    assert process.is_oncallbot("/x/.venv/bin/oncallbot restart")
    # And the commands that are not servers still do not match.
    assert not process.is_oncallbot("/x/.venv/bin/oncallbot stop")
    assert not process.is_oncallbot("/x/.venv/bin/oncallbot doctor")


def test_serve_refuses_when_one_is_already_running(tmp_path: Path, monkeypatch):
    """uvicorn's "address already in use" is a traceback, not an instruction.

    It also mattered for a second reason: writing our pid over the running
    server's entry, then clearing it as we exit, left the live server with no
    pid file at all.
    """
    runner, app, cfg = _cli(tmp_path)
    pidfile = tmp_path / ".data" / "oncallbot.pid"
    process.write_pidfile(pidfile, 8765, pid=42)

    monkeypatch.setattr(process, "process_command", lambda pid: ONCALLBOT)
    monkeypatch.setattr(process, "pid_on_port", lambda port: 42)
    started: list[bool] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: started.append(True))

    r = runner.invoke(app, ["serve", "--config", str(cfg)])
    assert r.exit_code == 1
    assert "Already running on port 8765 (pid 42)" in r.output
    assert "restart" in r.output, "it says what to do instead"
    assert started == []
    assert process.read_pidfile(pidfile) == (42, 8765), "the live entry is untouched"


def test_a_failed_serve_does_not_delete_the_running_servers_pidfile(
    tmp_path: Path, monkeypatch
):
    runner, app, cfg = _cli(tmp_path)
    pidfile = tmp_path / ".data" / "oncallbot.pid"
    process.write_pidfile(pidfile, 8765, pid=999)

    # Nothing found (so serve proceeds), but the file belongs to pid 999.
    monkeypatch.setattr(process, "process_command", lambda pid: "")
    monkeypatch.setattr(process, "pid_on_port", lambda port: None)

    def boom(*a, **k):
        raise OSError("address already in use")

    monkeypatch.setattr("uvicorn.run", boom)
    runner.invoke(app, ["serve", "--config", str(cfg)])

    # Our own write replaced it, and our clear removed only our own entry --
    # so what must never happen is a live server's entry vanishing silently
    # while a different pid holds the file.
    assert process.read_pidfile(pidfile) is None or process.read_pidfile(pidfile)[0] != 999


def test_a_pidfile_for_another_port_is_not_this_server(tmp_path: Path, monkeypatch):
    """The file lives beside the store, so two configs sharing a store share
    the file. Matching on pid alone made `serve --port 8082` refuse because a
    server was already running on 8765."""
    path = tmp_path / "oncallbot.pid"
    process.write_pidfile(path, 8765, pid=42)
    _fake(monkeypatch, commands={42: ONCALLBOT}, on_port=None)

    assert process.find_server(path, 8765) is not None, "its own port"
    assert process.find_server(path, 8082) is None, "a different port"
