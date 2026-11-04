"""Keep both existing private Redis selector contracts during image integration."""

import unittest

from tools.bazel.oci.runtime_smoke import _redis_tcp_port


class PrivateRedisSelectorsTest(unittest.TestCase):
    def test_default_keeps_tcp_disabled(self):
        self.assertEqual(_redis_tcp_port({}, {"hostname": "redis", "port": 6379}), 0)

    def test_both_existing_selectors_choose_the_declared_endpoint(self):
        instance = {"hostname": "127.0.0.1", "port": 6379}
        for config in ({"tcp_port": 6379}, {"loopback_tcp": True},
                       {"tcp_port": 6379, "loopback_tcp": True}):
            with self.subTest(config=config):
                self.assertEqual(_redis_tcp_port(config, instance), 6379)

    def test_rejects_external_or_mismatched_listener(self):
        for config, instance in (
            ({"tcp_port": 6379}, {"hostname": "0.0.0.0", "port": 6379}),
            ({"loopback_tcp": True}, {"hostname": "redis", "port": 6379}),
            ({"tcp_port": 6380}, {"hostname": "127.0.0.1", "port": 6379}),
            ({"tcp_port": 0, "loopback_tcp": True}, {"hostname": "127.0.0.1", "port": 6379}),
        ):
            with self.subTest(config=config, instance=instance), self.assertRaises(ValueError):
                _redis_tcp_port(config, instance)

    def test_rejects_invalid_types_ranges_and_privileged_loopback_mode(self):
        instance = {"hostname": "127.0.0.1", "port": 6379}
        for value in (True, "6379", -1, 65536):
            with self.subTest(port=value), self.assertRaises(ValueError):
                _redis_tcp_port({"tcp_port": value}, instance)
        with self.assertRaises(ValueError):
            _redis_tcp_port({"loopback_tcp": "yes"}, instance)
        for value in (True, "6379", 80, 65536):
            with self.subTest(owner_port=value), self.assertRaises(ValueError):
                _redis_tcp_port({"loopback_tcp": True}, {"hostname": "127.0.0.1", "port": value})


if __name__ == "__main__":
    unittest.main()
