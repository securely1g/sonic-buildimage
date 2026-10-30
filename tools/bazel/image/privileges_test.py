#!/usr/bin/env python3
import unittest

import privileges


class PrivilegesTest(unittest.TestCase):
    status = '\n'.join(name + ':\t0000000000000000' for name in
                       ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')) + '\nNoNewPrivs:\t1\nSeccomp:\t2\n'

    def test_accepts_nonroot_with_no_capabilities(self):
        self.assertEqual(privileges.check(self.status, 1000, [])['uid'], 1000)

    def test_rejects_root_and_capabilities_even_without_effective_caps(self):
        with self.assertRaisesRegex(ValueError, 'nonroot'):
            privileges.check(self.status, 0, [])
        for field in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'capabilities'):
                privileges.check(self.status.replace(field + ':\t0000000000000000', field + ':\t0000000000000001'), 1000, [])

    def test_requires_privilege_escalation_block_and_seccomp(self):
        for before, after in (('NoNewPrivs:\t1', 'NoNewPrivs:\t0'), ('Seccomp:\t2', 'Seccomp:\t0')):
            with self.assertRaisesRegex(ValueError, 'no-new-privileges'):
                privileges.check(self.status.replace(before, after), 1000, [])

    def test_rejects_exposed_socket_or_device(self):
        with self.assertRaisesRegex(ValueError, 'runtime socket'):
            privileges.check(self.status, 1000, ['/proc/self/status'])


if __name__ == '__main__':
    unittest.main()
