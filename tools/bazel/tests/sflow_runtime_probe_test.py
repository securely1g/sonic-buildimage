"""Exercise the Redis readiness check used by both installed sFlow images."""

from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parents[3]
PROBE = runpy.run_path(str(ROOT / "dockers/docker-sflow/bazel/runtime_probe.py"))


class SflowPortIndexesTest(unittest.TestCase):
    def test_waits_for_both_mapper_writes(self):
        complete = {"index": "1", "ifindex": "7"}
        state = Mock()
        state.hgetall.side_effect = [{}, {"index": "1"}, complete]
        with patch("time.sleep"):
            result = PROBE["wait_for_port_indexes"](state, "Ethernet0")
        self.assertEqual(result, complete)
        self.assertEqual(state.hgetall.call_args_list, [call("PORT_INDEX_TABLE|Ethernet0")] * 3)


if __name__ == "__main__":
    unittest.main()
