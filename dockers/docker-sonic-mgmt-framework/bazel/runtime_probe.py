#!/usr/bin/env python3
"""Exercise installed REST, CLI, configuration and native libraries in isolation."""

import configparser
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import redis


def require(value, message):
    if not value:
        raise AssertionError(message)


def command(*arguments, env=None, cwd=None):
    result = subprocess.run(arguments, env=env, cwd=cwd, capture_output=True, text=True, timeout=45)
    require(result.returncode == 0, repr(arguments) + "\n" + result.stdout + result.stderr)
    return result.stdout + result.stderr


def main():
    report = json.loads(Path(sys.argv[1]).read_text())
    variant = sys.argv[2]
    for name in report["images"][variant]["native_paths"]:
        item = report["native"][name]
        path = Path("/") / name
        require(hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"], "native bytes differ: " + name)
        loader = ["/lib64/ld-linux-x86-64.so.2"]
        if name.startswith("usr/sbin/cli/"):
            # Match the installed clish_start launcher's private library path.
            loader += ["--library-path", "/usr/local/lib:/usr/sbin/cli/.libs"]
        require("not found" not in command(*loader, "--list", str(path)), name)
    for name in ("requests", "urllib3", "six", "swsscommon.swsscommon", "sonic_py_common", "supervisor"):
        module = importlib.import_module(name)
        require(str(Path(module.__file__).resolve()).startswith(("/usr/lib/", "/usr/local/lib/")), name)
    supervisor = configparser.ConfigParser(interpolation=None)
    supervisor.read("/etc/supervisor/conf.d/supervisord.conf")
    require(supervisor["program:rest-server"]["command"] == "/usr/bin/rest-server.sh", "REST supervisor entry changed")
    require(supervisor["program:rest-server"]["dependent_startup_wait_for"] == "start:exited", "REST startup dependency changed")
    for name in ("/usr/bin/start.sh", "/usr/bin/rest-server.sh"):
        command("/bin/bash", "-n", name)
    command("/usr/bin/start.sh")
    require("managed by sonic-config-engine" in Path("/var/sonic/config_status").read_text(), "start hook failed")
    data = {"DEVICE_METADATA": {"localhost": {"hostname": "framework-ci"}},
            "REST_SERVER": {"default": {"port": "8443", "client_auth": "none"}}}
    rendered = command("sonic-cfggen", "-a", json.dumps(data), "-t", "/usr/share/sonic/templates/mgmt_vars.j2")
    # Match the existing startup script's conversion of cfggen's Python values.
    settings = json.loads(rendered.replace("'", '"'))
    require(settings["rest_server"] == data["REST_SERVER"]["default"], "management template failed")
    processes, logs = [], []
    with tempfile.TemporaryDirectory(prefix="framework-installed-") as temporary:
        root = Path(temporary)
        env = dict(os.environ, HOME=str(root), DB_CONFIG_PATH=str(root / "database_config.json"),
                   CVL_SCHEMA_PATH="/usr/sbin/schema", CVL_CFG_FILE="/usr/sbin/cvl_cfg.json",
                   YANG_MODELS_PATH="/usr/models/yang")
        database = json.loads(Path("/run/redis/sonic-db/database_config.json").read_text())
        redis_socket = str(root / "redis.sock")
        for instance in database["INSTANCES"].values():
            instance.update(hostname="127.0.0.1", port=0, unix_socket_path=redis_socket)
        for name, number in (("LOGLEVEL_DB", 3), ("ERROR_DB", 8), ("EVENT_DB", 9)):
            database["DATABASES"].setdefault(name, {"id": number, "separator": "|", "instance": "redis"})
        (root / "database_config.json").write_text(json.dumps(database))

        def start(name, arguments):
            log = (root / (name + ".log")).open("w+")
            logs.append(log)
            process = subprocess.Popen(arguments, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
            return process

        def details():
            return "\n".join(path.name + ":\n" + path.read_text() for path in root.glob("*.log"))

        try:
            fixture = Path("/probe/redis")
            start("redis", [str(fixture / "usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"), "--library-path",
                  str(fixture / "usr/lib/x86_64-linux-gnu"), str(fixture / "usr/bin/redis-server"),
                  "--port", "0", "--unixsocket", redis_socket, "--save", "", "--appendonly", "no",
                  "--databases", "32", "--dir", str(root)])
            client = redis.Redis(unix_socket_path=redis_socket, db=4, socket_timeout=1)
            deadline = time.monotonic() + 20
            while True:
                try:
                    if client.ping():
                        break
                except redis.RedisError:
                    require(time.monotonic() < deadline, details())
                    time.sleep(0.1)
            client.hset("DEVICE_METADATA|localhost", mapping={"hostname": "framework-ci"})
            command("/usr/sbin/generate_cert", "--host", "localhost,127.0.0.1", "--ecdsa-curve", "P256",
                    "--duration", "1h", env=env, cwd=root)
            context = ssl.create_default_context(cafile=str(root / "cert.pem"))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            service = start("rest", ["/usr/sbin/rest_server", "-port", str(port), "-cert", str(root / "cert.pem"),
                            "-key", str(root / "key.pem"), "-client_auth", "none", "-logtostderr"])
            base = "https://127.0.0.1:" + str(port)
            deadline = time.monotonic() + 30
            while True:
                try:
                    with opener.open(base + "/restconf/yang-library-version", timeout=1) as response:
                        require(json.load(response)["ietf-restconf:yang-library-version"] == "2016-06-21", "REST version differs")
                    break
                except (OSError, urllib.error.URLError):
                    require(service.poll() is None and time.monotonic() < deadline, details())
                    time.sleep(0.1)
            with opener.open(base + "/.well-known/host-meta", timeout=5) as response:
                require(b"href='/restconf'" in response.read(), "REST discovery differs")
            with opener.open(base + "/restconf/operations", timeout=5) as response:
                require("sonic-show-techsupport:sonic-show-techsupport-info" in json.load(response)["operations"], "generated RPC absent")
            with opener.open(base + "/ui/index.html", timeout=5) as response:
                require(response.read() == Path("/etc/rest_server/ui/index.html").read_bytes(), "installed UI differs")
            cli = "/usr/sbin/cli"
            cli_env = dict(env, SYSTEM_NAME="sonic", PYTHONNOUSERSITE="1")
            launcher = cli + "/clish_start"
            require("2.1.4" in command(launcher, "-v", env=cli_env, cwd=root), "wrong klish version")
            command(launcher, "-k", "-c", "show tech-support", env=cli_env, cwd=root)
            rejected = subprocess.run([launcher, "-k", "-c", "not-a-real-sonic-command"], env=cli_env,
                                      cwd=root, capture_output=True, text=True, timeout=30)
            require(rejected.returncode == 255 and "Syntax error" in rejected.stdout + rejected.stderr,
                    "invalid CLI command was accepted")
            print(json.dumps({"status": "passed", "variant": variant,
                              "source_elf_loader_checks": len(report["images"][variant]["native_paths"]),
                              "checks": ["installed Python dependencies", "supervisor startup contract", "start hook",
                                         "cfggen management settings", "private Redis", "certificate generation", "verified TLS",
                                         "RESTCONF discovery/version", "generated RPC", "installed UI", "installed CLI command-tree parsing"],
                              "limits": "Private fixtures with authentication disabled; no host PAM/TACACS, hardware or VS boot acceptance"}))
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for log in logs:
                log.close()
            if "client" in locals():
                client.close()


if __name__ == "__main__":
    main()
