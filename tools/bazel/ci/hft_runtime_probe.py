"""Exercise HFT's installed entrypoint, supervisor, countersyncd and readiness hook with private Redis."""

import json
import os
import re
import shutil
import sys
from pathlib import Path
import subprocess
import tempfile
import time

import redis


ENTRYPOINT = "/usr/bin/docker_init.sh"
HOOK = "/usr/bin/readiness_probe_hook"


def wait_for(predicate, process, log, message):
    deadline = time.monotonic() + 45
    while not predicate():
        if process.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError(message + "\n" + log.read_text(errors="replace")[-8000:])
        time.sleep(0.15)


def debug_source(build_id):
    binary = Path("/usr/bin/countersyncd")
    symbols = Path("/usr/lib/debug/.build-id") / build_id[:2] / (build_id[2:] + ".debug")
    assert symbols.is_file(), "Missing detached countersyncd symbols"
    with tempfile.TemporaryDirectory() as temporary:
        isolated = Path(temporary) / binary.name
        shutil.copyfile(binary, isolated)
        source = re.compile(r'Line [1-9][0-9]* of "[^"]*crates/countersyncd/src/main\.rs" starts at address')
        def lookup(executable, debug_directory):
            result = subprocess.run(["gdb", "-nx", "-nh", "--batch", "-iex", "set auto-load off",
                                     "-iex", "set debuginfod enabled off", "-iex", "set debug-file-directory " + debug_directory,
                                     "-ex", "file " + str(executable), "-ex", "info line countersyncd::main"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=45)
            return source.search(result.stdout)
        assert not lookup(isolated, temporary), "countersyncd still contains packaged source lines"
        match = lookup(binary, "/usr/lib/debug")
        assert match, "GDB could not resolve the countersyncd Rust main function"
        return {"build_id": build_id, "source": match.group()}


def main():
    assert os.environ["IMAGE_VERSION"] and os.environ["DEBIAN_FRONTEND"] == "noninteractive"
    help_text = subprocess.check_output([ENTRYPOINT, "--help"], text=True, timeout=10)
    assert "Usage: countersyncd" in help_text and "--enable-counter-db" in help_text
    assert subprocess.run([HOOK], timeout=5).returncode == 3
    socket = "/run/redis/redis.sock"
    fixture = Path("/probe/redis/usr")
    library = fixture / "lib/x86_64-linux-gnu"
    (Path("/var/log") / "supervisor").mkdir(exist_ok=True)
    database = redis.Redis(unix_socket_path=socket, db=6, socket_timeout=1)

    def listening():
        try:
            return database.ping()
        except redis.ConnectionError:
            return False

    with tempfile.TemporaryDirectory() as temp:
        log = Path(temp) / "hft.log"
        with log.open("wb") as output:
            server = subprocess.Popen([str(library / "ld-linux-x86-64.so.2"), "--library-path", str(library),
                                       str(fixture / "bin/redis-server"), "--port", "0", "--unixsocket", socket,
                                       "--save", "", "--appendonly", "no", "--databases", "32",
                                       "--notify-keyspace-events", "KEA"], stdout=output, stderr=output)
            try:
                wait_for(listening, server, log, "Private HFT Redis did not start")
                service = subprocess.Popen([ENTRYPOINT, "--stats-interval", "2"],
                                           env=os.environ | {"COUNTERSYNCD_ENABLE_STATS": "true",
                                                             "COUNTERSYNCD_STATS_INTERVAL": "17"},
                                           stdout=output, stderr=output)
                try:
                    wait_for(lambda: subprocess.run([HOOK], timeout=3).returncode == 0,
                             service, log, "HFT countersyncd did not become ready")
                    pid = int(subprocess.check_output(["supervisorctl", "pid", "countersyncd"], timeout=5))
                    proc = Path("/proc") / str(pid)
                    assert (proc / "exe").readlink() == Path("/usr/bin/countersyncd")
                    args = (proc / "cmdline").read_bytes().split(b"\0")
                    assert b"--enable-stats" in args and b"--stats-interval=2" in args, args
                    # A disabled hardware-independent profile still traverses the real
                    # STATE_DB subscriber and its IPFIX actor channel.
                    profile = "vs-hft-probe|PORT"
                    database.hset("HIGH_FREQUENCY_TELEMETRY_SESSION_TABLE|" + profile,
                                  mapping={"stream_status": "disabled", "session_type": "ipfix"})
                    wait_for(lambda: "Successfully sent IPFix templates for session: " + profile in
                             log.read_text(errors="replace"), service, log, "countersyncd did not process the STATE_DB row")
                    assert Path("/var/sonic/config_status").is_file()
                finally:
                    service.terminate()
                    service.wait(timeout=20)
            finally:
                database.close()
                server.terminate()
                server.wait(timeout=10)
    receipt = {"status": "passed", "service": "countersyncd", "readiness": "RUNNING",
               "state_db_profile_processed": True}
    if len(sys.argv) == 2:
        receipt["debug"] = debug_source(sys.argv[1])
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
