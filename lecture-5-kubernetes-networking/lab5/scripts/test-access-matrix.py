#!/usr/bin/env python3
"""Local checks of result interpretation; no Kubernetes access."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('matrix', Path(__file__).with_name('check-access-matrix.py'))
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


class Verdicts(unittest.TestCase):
    def row(self, statuses, expected, controls=True):
        return matrix.classify_row([{'status': s} for s in statuses], expected, controls)

    def test_refused_or_exec_failure_is_not_a_policy_denial(self):
        self.assertEqual(self.row(['ERROR'], 'DENY'), ('ERROR', 0, False))

    def test_timeout_needs_healthy_controls(self):
        self.assertEqual(self.row(['TIMEOUT'], 'DENY'), ('DENY', 1, True))
        self.assertEqual(self.row(['TIMEOUT'], 'DENY', False), ('UNVERIFIED', 1, False))

    def test_one_leaking_replica_fails_the_group(self):
        self.assertEqual(self.row(['TIMEOUT', 'ALLOW'], 'DENY'), ('MIXED', 1, False))

    def test_one_unreachable_replica_fails_the_group(self):
        self.assertEqual(self.row(['ALLOW', 'TIMEOUT'], 'ALLOW'), ('MIXED', 1, False))


class Controls(unittest.TestCase):
    def probe(self, ip, status='ALLOW', port='5432'):
        return {'args': ['tcp', ip, port], 'status': status, 'detail': status}

    def test_failed_external_control_does_not_invalidate_postgres(self):
        pg = self.probe('10.244.0.10')
        external = self.probe('1.1.1.1', port='443')
        groups = [
            {'expected': 'DENY', 'results': [self.probe('10.244.0.10', 'TIMEOUT')]},
            {'expected': 'DENY', 'results': [self.probe('1.1.1.1', 'TIMEOUT', '443')]},
        ]
        matrix.evaluate_groups(groups, [pg, dict(external, status='TIMEOUT')], [pg, external], {})
        self.assertTrue(groups[0]['ok'])
        self.assertEqual(groups[0]['actual'], 'DENY')
        self.assertFalse(groups[1]['ok'])
        self.assertEqual(groups[1]['actual'], 'UNVERIFIED')

    def test_control_must_cover_the_same_address_port_and_both_phases(self):
        control = self.probe('10.244.0.10')
        for target in (self.probe('10.244.0.11'), self.probe('10.244.0.10', port='8000')):
            self.assertNotIn(matrix.target_key(target, {}), matrix.target_controls([control], [control], {}))
        self.assertEqual(matrix.target_controls([control], [], {}), set())

    def test_service_name_control_matches_service_vip(self):
        aliases = {'db.shop.svc.cluster.local': '10.96.0.10'}
        named = self.probe('db.shop.svc.cluster.local.')
        numeric = self.probe('10.96.0.10')
        self.assertEqual(matrix.target_key(named, aliases), matrix.target_key(numeric, aliases))

    def test_udp_control_does_not_validate_tcp_dns(self):
        udp = {'args': ['dns', '10.96.0.10', 'udp', 'shop.', '10.96.0.20'], 'status': 'ALLOW'}
        tcp = {'args': ['dns', '10.96.0.10', 'tcp', 'shop.', '10.96.0.20'], 'status': 'ALLOW'}
        self.assertNotIn(matrix.target_key(tcp, {}), matrix.target_controls([udp], [udp], {}))

    def test_only_external_control_timeouts_are_retried(self):
        runner = matrix.Matrix(None)
        timeout = {'status': 'TIMEOUT', 'detail': 'timeout'}
        allowed = {'status': 'ALLOW', 'detail': 'connected'}
        with patch.object(runner, 'probe', side_effect=[timeout, allowed]) as probe, patch.object(matrix.time, 'sleep'):
            result = runner.checked_probe({'external_control': True})
            self.assertEqual(result['status'], 'ALLOW')
            self.assertEqual(len(result['control_attempts']), 2)
            self.assertEqual(probe.call_count, 2)
        with patch.object(runner, 'probe', return_value=timeout) as probe:
            self.assertEqual(runner.checked_probe({})['status'], 'TIMEOUT')
            self.assertEqual(probe.call_count, 1)

    def test_repeated_timeout_and_exec_error_still_fail(self):
        runner = matrix.Matrix(None)
        with patch.object(runner, 'probe', return_value={'status': 'TIMEOUT', 'detail': 'timeout'}) as probe, patch.object(matrix.time, 'sleep'):
            result = runner.checked_probe({'external_control': True})
            self.assertEqual(result['status'], 'TIMEOUT')
            self.assertEqual(probe.call_count, 3)
        with patch.object(runner, 'probe', return_value={'status': 'ERROR', 'detail': 'kubectl failed'}) as probe:
            self.assertEqual(runner.checked_probe({'external_control': True})['status'], 'ERROR')
            self.assertEqual(probe.call_count, 1)


if __name__ == '__main__':
    unittest.main()
