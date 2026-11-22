"""Retain command output and timings separately from queried artifact paths."""

import re
import shutil
import subprocess
import sys
import time
from time import sleep


def transient_cache_failure(returncode, diagnostics):
    """Fail closed unless every primary error is a transient remote-cache error."""
    if returncode not in (1, 34) or len(diagnostics) > 2 * 1024 * 1024:
        return False
    diagnostics = re.sub(r"\x1b\[[0-9;]*m", "", diagnostics)
    if re.search(
            r"checksum|digest mismatch|unauthenticated|permission.denied|unauthorized|"
            r"forbidden|certificate|PKIX|SSLHandshakeException|no space left|disk full|"
            r"\b(?:401|403|NOT_FOUND)\b|undefined reference|"
            r"\S+:\d+(?::\d+)?:\s*(?:fatal )?error:", diagnostics, re.IGNORECASE):
        return False
    transport = re.compile(
        r"connection (?:refused|reset|closed)|connectexception|"
        r"\bEOF\b|timed out|timeout(?:exception)?|\bUNAVAILABLE\b|"
        r"\bHTTP[^\n]*\b(?:429|500|502|503|504)\b", re.IGNORECASE)
    cache = re.compile(
        r"remote (?:execution )?capabilities|remote cache|remote-cache|"
        r"remote.*(?:download|upload)|(?:download|upload).*blob", re.IGNORECASE)
    errors = re.findall(r"(?m)^.*\bERROR:.*(?:\n(?!.*\bERROR:).*)*", diagnostics)
    primary = False
    for error in errors:
        headline = error.splitlines()[0]
        if re.search(r"ERROR: Build did NOT complete successfully", headline):
            continue
        if "Unable to write all BEP events" in headline:
            if not transport.search(error):
                return False
            continue
        if not cache.search(headline) or not transport.search(error):
            return False
        primary = True
    return primary


def transient_fetch_failure(stderr):
    """Retry explicit repository transport failures, never unrelated diagnostics."""
    transient = re.compile(
        r"(?:fetch_repo:|Error downloading).*https?://.*"
        r"(?:\bEOF\b|connection reset by peer|connection refused|"
        r"i/o timeout|connect(?:ion)? timed out|TLS handshake timeout|"
        r"temporary failure in name resolution|"
        r"\b(?:429 Too Many Requests|50[0234] (?:Internal Server Error|Bad Gateway|"
        r"Service Unavailable|Gateway Timeout))\b)", re.I)
    if not transient.search(stderr) or re.search(
            r"checksum|SECURITY ERROR|certificate|invalid version|unknown revision", stderr, re.I):
        return False
    for line in stderr.splitlines():
        if re.search(r"fetch_repo:|Error downloading", line, re.I) and not transient.search(line):
            return False
        if not re.search(r"\b(?:ERROR|Error in fail|error|fatal error|FAILED):", line):
            continue
        if transient.search(line):
            continue
        if "An error occurred during the fetch of repository" in line:
            continue
        if "Analysis of target" in line and "failed" in line:
            continue
        if line.strip() == "ERROR: Build did NOT complete successfully":
            continue
        return False
    return True


def retry_remote_cache(run, *, diagnostics=None, on_retry=None, retry_fetch=False):
    """Retry the same invocation twice; never change cache or execution policy."""
    for attempt, delay in enumerate((10, 30, None), 1):
        try:
            return run()
        except subprocess.CalledProcessError as error:
            text = (diagnostics(error) if diagnostics is not None
                    else (error.stdout or "") + (error.stderr or ""))
            cache_failure = transient_cache_failure(error.returncode, text)
            fetch_failure = (retry_fetch and error.returncode == 1
                             and len(text) <= 2 * 1024 * 1024
                             and transient_fetch_failure(text))
            if delay is None or not (cache_failure or fetch_failure):
                raise
            if on_retry is not None:
                on_retry(attempt, delay)
            if cache_failure:
                print(f"Transient Bazel remote-cache failure; retrying in {delay}s "
                      f"(attempt {attempt + 1}/3)", file=sys.stderr, flush=True)
                sleep(delay)
            else:
                print(f"Transient Bazel repository fetch failure; retrying attempt "
                      f"{attempt + 1}/3 in {delay}s.", flush=True)
                time.sleep(delay)


def execute(command, directory, receipt, name, *, cwd, output_path=None,
            stream=False, retry_fetch=False):
    """Keep diagnostics separate from query results and optional private output.

    Large or sensitive query results can go directly to an already-created
    file. That stdout is neither logged nor printed; the caller owns its mode
    and lifetime. Ordinary queries still return only stdout for path lookup.
    Streaming builds retain live progress and return no queried paths.
    Bazel callers can opt into three attempts for explicit transient repository
    fetch failures; attempt logs and total wall time remain in the receipt.
    """
    if stream and output_path is not None:
        raise ValueError("private query output cannot be streamed")
    started = time.monotonic()
    entry = {"argv": command, "log": name + ".log"}
    attempts = []

    def run():
        attempt_started = time.monotonic()
        if stream:
            with (directory / entry["log"]).open("w") as log, \
                    subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT) as process:
                try:
                    for line in process.stdout:
                        log.write(line)
                        log.flush()
                        print(line, end="", flush=True)
                    status = process.wait()
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
            diagnostics = ""
            if status:
                with (directory / entry["log"]).open() as log:
                    diagnostics = log.read(2 * 1024 * 1024 + 1)
            result = subprocess.CompletedProcess(command, status, stdout="", stderr=diagnostics)
        elif output_path is None:
            result = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
        else:
            with output_path.open("w") as output:
                result = subprocess.run(command, cwd=cwd, text=True,
                                        stdout=output, stderr=subprocess.PIPE)
        stdout = result.stdout or ""
        if not stream:
            (directory / entry["log"]).write_text(stdout + (result.stderr or ""))
            print(stdout, end="", flush=True)
            print(result.stderr or "", end="", flush=True)
        entry["returncode"] = result.returncode
        attempts.append({"returncode": result.returncode,
                         "elapsed_seconds": time.monotonic() - attempt_started,
                         "log": entry["log"]})
        result.check_returncode()
        return stdout

    def retain_attempt(attempt, delay):
        log = f"{name}.attempt-{attempt}.log"
        shutil.copyfile(directory / entry["log"], directory / log)
        attempts[-1].update(log=log, retry_delay_seconds=delay)

    try:
        return retry_remote_cache(run, on_retry=retain_attempt, retry_fetch=retry_fetch)
    finally:
        if attempts:
            entry["elapsed_seconds"] = time.monotonic() - started
            if len(attempts) > 1 or "retry_delay_seconds" in attempts[0]:
                entry["attempts"] = attempts
            receipt["commands"].append(entry)
