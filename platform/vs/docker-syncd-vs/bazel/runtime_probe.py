#!/usr/bin/env python3
"""Run the installed VS startup chain against private Redis, without forwarding."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from xmlrpc.client import ServerProxy

import redis
from supervisor.xmlrpc import SupervisorTransport

sys.path.insert(0, str(Path(__file__).parent))
import runtime_smoke


def require(value, message):
    if not value:
        raise AssertionError(message)


def command(argv, timeout=30):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    require(result.returncode == 0, repr(argv) + "\n" + result.stdout + result.stderr)
    return result.stdout


def wait_until(callback, message, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = callback()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(message)


def descendants(pid):
    yield pid
    children = Path(f"/proc/{pid}/task/{pid}/children")
    if children.exists():
        for child in children.read_text().split():
            yield from descendants(int(child))


@contextlib.contextmanager
def private_redis(database):
    """Reuse the declared helper closure and prove TCP/Unix reach this process."""
    settings = json.loads(database.read_text())
    require(all(settings["DATABASES"][name]["id"] == number and
                settings["DATABASES"][name]["instance"] == "redis"
                for name, number in (("CONFIG_DB", 4), ("STATE_DB", 6), ("ASIC_DB", 1), ("FLEX_COUNTER_DB", 5))),
            "original Redis database identities changed")
    settings["INSTANCES"]["redis"].update(hostname="127.0.0.1", port=6379,
                                         unix_socket_path="/run/redis/redis.sock")
    database.write_text(json.dumps(settings))
    root = Path("/probe/redis")
    binary = root / "usr/bin/redis-server"
    loader = root / "usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
    argv = [str(loader), "--library-path", str(root / "usr/lib/x86_64-linux-gnu"), str(binary),
            "--bind", "127.0.0.1", "--port", "6379", "--unixsocket", "/run/redis/redis.sock",
            "--unixsocketperm", "700", "--save", "", "--appendonly", "no", "--databases", "32",
            "--dir", "/tmp"]
    tcp = redis.Redis(host="127.0.0.1", port=6379, socket_timeout=1)
    unix = redis.Redis(unix_socket_path="/run/redis/redis.sock", socket_timeout=1)
    with Path("/tmp/syncd-redis.log").open("w") as log:
        process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
        try:
            def ready():
                require(process.poll() is None, "private Redis exited")
                try:
                    return (tcp.ping() and unix.ping() and
                            tcp.info("server")["process_id"] == unix.info("server")["process_id"] == process.pid)
                except redis.exceptions.RedisError:
                    return False
            wait_until(ready, "private TCP/Unix Redis did not become ready")
            yield {"sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                   "database_config_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
                   "loopback_tcp_and_unix_same_process": True,
                   "scope": "Declared private test helper; absent from production layers"}
        finally:
            tcp.close(); unix.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()


def main():
    for name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "PYTHONPATH"):
        os.environ.pop(name, None)
    report = json.loads(Path(sys.argv[1]).read_text())
    variant = sys.argv[2]
    expected = report["images"][variant]
    for name in expected["native_paths"]:
        path = Path("/") / name
        require(hashlib.sha256(path.read_bytes()).hexdigest() == report["native"][name]["sha256"], "native bytes differ: " + name)
        run = runtime_smoke.command(["/lib64/ld-linux-x86-64.so.2", "--list", str(path)])
        pair = next(item for item in report["source_debug_pairs"] if item["path"] == name)
        require(runtime_smoke.loader_result(run, pair.get("needed", [])), "installed loader failed: " + name + repr(run))
    debug_checks = []
    if variant.endswith("-dbg"):
        targets = {"usr/bin/syncd", "usr/bin/syncd_dash", "usr/bin/pi_CLI_dummy", "usr/bin/sonic-db-cli"}
        selected = [pair for pair in report["source_debug_pairs"] if pair["path"] in targets]
        require(targets == {pair["path"] for pair in selected}, "missing installed debugger contracts")
        debug_checks = [runtime_smoke.debug_pair(pair) for pair in selected]
        for item in debug_checks:
            item["with_symbols"]["source"] = Path(item["with_symbols"]["source"]).name
        require("Huge version" in command(["/usr/bin/vim", "--version"]), "debug vim is not the declared full Vim")
    else:
        require("Tiny version" in command(["/usr/bin/vim", "--version"]), "runtime Vim changed")

    hwsku = Path("/usr/share/sonic/hwsku")
    hwsku.mkdir(parents=True, exist_ok=True)
    for name in ("sai.profile", "lanemap.ini", "coreportindexmap.ini", "fabriclanemap.ini"):
        (hwsku / name).write_bytes((Path("/probe/hwsku") / name).read_bytes())
    profile = (hwsku / "sai.profile").read_text()
    command(["/usr/bin/start.sh"])
    require(Path("/etc/sai.d/sai.profile").read_text() == profile, "original copied profile differs")
    database = Path("/run/redis/sonic-db/database_config.json")
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(Path("/probe/redis/database_config.json").read_bytes())
    Path("/etc/sonic/sonic_version.yml").write_text("asic_type: vs\n")
    with private_redis(database) as redis_report:
        config = json.loads(database.read_text())
        instance = config["INSTANCES"]["redis"]
        connection = redis.Redis(unix_socket_path=instance["unix_socket_path"], db=4, decode_responses=True)
        connection.set("CONFIG_DB_INITIALIZED", "1")
        connection.hset("DEVICE_METADATA|localhost", mapping={
            "hostname": "syncd-probe", "platform": "x86_64-kvm_x86_64-r0", "hwsku": "SONiC-VM",
            "mac": "02:00:00:00:00:01", "type": "ToRRouter", "switch_type": "switch",
            "synchronous_mode": "enable", "enable_per_port_counter_discovery": "false"})
        connection.hset("FEATURE|syncd", mapping={"state": "enabled", "auto_restart": "enabled"})
        # Exercise both unchanged start.sh branches; syncd_init_common uses the
        # original HW profile directly, so it must not be replaced by this render.
        (hwsku / "sai.profile.j2").write_text(profile + "# {{ DEVICE_METADATA['localhost']['hostname'] }}\n")
        command(["/usr/bin/start.sh"])
        require(Path("/etc/sai.d/sai.profile").read_text() == profile + "# syncd-probe\n", "original cfggen profile rendering differs")
        variables = json.loads(command(["sonic-cfggen", "-d", "-y", "/etc/sonic/sonic_version.yml",
                                        "-t", "/usr/share/sonic/templates/swss_vars.j2"]))
        require(variables["asic_type"] == "vs" and variables["switch_type"] == "switch" and
                variables["synchronous_mode"] == "enable", "unexpected original startup configuration")
        for name in ("/var/log/supervisor", "/var/spool/rsyslog"):
            Path(name).mkdir(parents=True, exist_ok=True)
        log_path = Path("/tmp/syncd-entrypoint.log")
        with log_path.open("w") as log:
            process = subprocess.Popen(["/usr/local/bin/supervisord"], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                def running_syncd():
                    require(process.poll() is None, "supervisor exited:\n" + log_path.read_text())
                    selected = []
                    for pid in descendants(process.pid):
                        executable = Path(f"/proc/{pid}/exe")
                        try:
                            if executable.exists() and os.path.samefile(executable, "/usr/bin/syncd"):
                                selected.append(pid)
                        except FileNotFoundError:
                            pass
                    require(len(selected) <= 1, "startup launched multiple Syncd processes")
                    return selected[0] if selected else 0
                pid = wait_until(running_syncd, "original startup did not execute installed Syncd")
                args = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
                wanted = ["/usr/bin/syncd"]
                if os.access("/usr/bin/dsserve", os.X_OK):
                    wanted.append("--diag")
                wanted += ["-u", "-s"]
                # Preserve the original jq missing-field behavior (literal null).
                bulk = variables.get("supporting_bulk_counter_groups", "null")
                if bulk != "":
                    wanted += ["-B", str(bulk)]
                wanted += ["-l", "-p", "/usr/share/sonic/hwsku/sai.profile", "-w", "30000000"]
                require(args == wanted, "original Syncd argv differs: " + repr(args))
                required_dbs = {config["DATABASES"][name]["id"] for name in ("ASIC_DB", "FLEX_COUNTER_DB")}
                wait_until(lambda: {int(item["db"]) for item in connection.client_list()} >= required_dbs,
                           "Syncd did not connect to the original ASIC/FLEX databases")
                for _ in range(10):
                    time.sleep(0.5)
                    require(running_syncd() == pid, "original Syncd startup did not remain alive")
                transport = SupervisorTransport(None, None, "unix:///var/run/supervisor.sock")
                with ServerProxy("http://localhost", transport=transport) as rpc:
                    services = {item["name"]: item for item in rpc.supervisor.getAllProcessInfo()}
                states = {}
                for name in ("start", "dependent-startup", "supervisor-proc-exit-listener", "rsyslogd", "syncd"):
                    status = services.get(name, {})
                    running = status.get("statename") == "RUNNING"
                    completed = status.get("statename") == "EXITED" and status.get("exitstatus") == 0
                    require((completed if name == "start" else running or (name == "dependent-startup" and completed)),
                            "required original service failed: " + name + repr(status))
                    states[name] = {"state": status["statename"], "exitstatus": status["exitstatus"]}
            except Exception as error:
                raise AssertionError(str(error) + "\n" + log_path.read_text()) from error
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
    print(json.dumps({"status": "passed", "variant": variant, "native_files": len(expected["native_paths"]),
                      "debug_checks": debug_checks, "private_redis": redis_report, "service_states": states,
                      "actual_entrypoint": True, "actual_syncd": True, "startup_arguments": args,
                      "profile_copy_and_render": True, "stable_process": True,
                      "scope": "Original regular VS service startup and software dependencies; DASH source/debug closure only; no forwarding or hardware"}))


if __name__ == "__main__":
    main()
