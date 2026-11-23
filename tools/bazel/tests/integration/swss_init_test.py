"""Check the non-ASAN Orchagent init script, either freshly rendered or installed by VS."""
from pathlib import Path
import subprocess
import sys


def verify(script):
    subprocess.run(["bash", "-n"], input=script, check=True)
    source = script.decode()
    required = ('"${ASIC_VENDOR:-unknown}', "/etc/sonic/constants.yml", "sonic-cfggen $CFGGEN_PARAMS",
                "sonic-db-cli -s CONFIG_DB HGET", "$NAMESPACE_ID", "/usr/share/sonic/hwsku/hwsku-init",
                "/etc/swss/config.d/switch.json", "exec /usr/local/bin/supervisord")
    if "ENABLE_ASAN" in source or "{%" in source or not source.endswith("\n") or not all(x in source for x in required):
        raise ValueError("Invalid rendered Orchagent startup script")


if __name__ == "__main__":
    verify(Path(sys.argv[1]).read_bytes())
