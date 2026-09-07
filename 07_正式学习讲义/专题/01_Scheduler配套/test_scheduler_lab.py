"""Offline contract tests only; no Kubernetes integration is implied."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch
import scheduler_lab as s


class ContractTests(unittest.TestCase):
    def test_cpu_whole(self):
        self.assertEqual(s.cpu_m('4'), 4000)

    def test_cpu_milli(self):
        self.assertEqual(s.cpu_m('250m'), 250)

    def test_cpu_decimal(self):
        self.assertEqual(s.cpu_m('1.5'), 1500)

    def test_cpu_round_up(self):
        self.assertEqual(s.cpu_m('1000001n'), 2)

    def test_cpu_negative_rejected(self):
        with self.assertRaises(ValueError): s.cpu_m('-1')

    def test_shape_one_less_than_capacity_two_exceed(self):
        for size in (1000, 2000, 4000, 8000, 64000):
            shape = s.shape_cpu(size)
            self.assertLess(shape, size)
            self.assertGreater(shape * 2, size)

    def test_small_worker_rejected(self):
        with self.assertRaises(ValueError): s.shape_cpu(500)

    def test_pod_is_targeted_without_bypassing_scheduler(self):
        p = s.pod('test', 'owner')
        self.assertEqual(p['spec']['nodeSelector'], {s.TARGET: 'owner'})
        self.assertNotIn('nodeName', p['spec'])
        self.assertFalse(p['spec']['automountServiceAccountToken'])

    def test_pod_explicit_requests(self):
        p = s.pod('test', 'owner', '2201m')
        self.assertEqual(p['spec']['containers'][0]['resources']['requests'], {'cpu': '2201m', 'memory': '64Mi'})

    def test_bound_not_equal_ready(self):
        p = {'spec': {'nodeName': 'n'}}
        self.assertTrue(s.is_bound(p))
        self.assertFalse(s.has_condition(p, 'Ready', 'True'))

    def test_unschedulable_structured_condition(self):
        p = {'status': {'conditions': [{'type': 'PodScheduled', 'status': 'False', 'reason': 'Unschedulable'}]}}
        self.assertTrue(s.unschedulable(p))
        p['spec'] = {'nodeName': 'n'}
        self.assertFalse(s.unschedulable(p))

    def test_pending_phase_alone_not_unschedulable(self):
        self.assertFalse(s.unschedulable({'status': {'phase': 'Pending'}}))

    def test_missing_state_stops_before_any_cli(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(s, 'command') as command:
            with self.assertRaises(RuntimeError): s.Lab(Path(directory)).verify()
            command.assert_not_called()

    def test_non_lab_name_cannot_generate_kubectl(self):
        lab = s.Lab(Path('/tmp/not-used'))
        lab.state = {'name': 'production'}
        with patch.object(s, 'command') as command:
            with self.assertRaises(RuntimeError): lab.k('get', 'nodes')
            command.assert_not_called()

    def test_every_kubectl_call_pins_context_and_kubeconfig(self):
        lab = s.Lab(Path('/tmp/not-used'))
        lab.state = {'name': 'scheduler-study-0123abcd'}
        with patch.object(s, 'command', return_value='ok') as command:
            self.assertEqual(lab.k('get', 'nodes'), 'ok')
            argv = command.call_args.args[0]
            self.assertEqual(argv[argv.index('--context') + 1], 'kind-scheduler-study-0123abcd')
            self.assertEqual(argv[argv.index('--kubeconfig') + 1], str(lab.kubeconfig))

    def test_cluster_uid_change_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = s.Lab(Path(directory))
            state = {'name': 'scheduler-study-0123abcd', 'cluster_uid': 'original', 'node': 'worker'}
            lab.state_path.write_text(json.dumps(state))
            lab.kubeconfig.write_text('test fixture; not a real config')
            with patch.object(s, 'command', return_value=state['name']), patch.object(lab, 'obj', return_value={'metadata': {'uid': 'different'}}):
                with self.assertRaisesRegex(RuntimeError, 'identity changed'): lab.verify()

    def test_init_refuses_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'keep').write_text('existing content')
            with patch.object(s, 'command') as command:
                with self.assertRaises(RuntimeError): s.Lab(path).init('kindest/node:v1.34.0', s.IMAGE)
                command.assert_not_called()

    def test_destroy_requires_exact_name(self):
        lab = s.Lab(Path('/tmp/not-used'))
        lab.state = {'name': 'scheduler-study-0123abcd'}
        with patch.object(lab, 'verify'), patch.object(s, 'command') as command:
            with self.assertRaises(RuntimeError): lab.destroy('yes')
            command.assert_not_called()

    def test_corrected_score_arithmetic(self):
        self.assertEqual((8000-2000-1000)*100//8000, 62)
        self.assertEqual((8000-4500-1000)*100//8000, 31)

    def test_snapshot_keeps_cross_namespace_node_pods(self):
        lab = s.Lab(Path('/tmp/not-used'))
        lab.state = {'node': 'worker'}
        lab.evidence = Path('/tmp/not-used')
        system_pod = {'metadata': {'name': 'system-agent', 'namespace': 'kube-system'},
                      'spec': {'nodeName': 'worker',
                               'containers': [{'resources': {'requests': {'cpu': '100m'}}}]}}

        def objects(*args):
            if args == ('pods', '-A', '--field-selector', 'spec.nodeName=worker'):
                return {'items': [system_pod]}
            return {}

        with patch.object(lab, 'obj', side_effect=objects), patch.object(Path, 'open', mock_open()) as output:
            lab.snapshot('test')
            record = json.loads(output().write.call_args.args[0])
        self.assertEqual(record['node_pods']['items'], [system_pod])
        self.assertEqual(record['stage'], 'test')

    def test_single_replica_rollout_complete(self):
        d = {'metadata': {'generation': 3}, 'spec': {'replicas': 1},
             'status': {'observedGeneration': 3, 'updatedReplicas': 1,
                        'replicas': 1, 'availableReplicas': 1}}
        self.assertTrue(s.single_replica_rollout_complete(d))

    def test_rollout_rejects_stale_old_or_unavailable_replicas(self):
        base = {'metadata': {'generation': 3}, 'spec': {'replicas': 1},
                'status': {'observedGeneration': 3, 'updatedReplicas': 1,
                           'replicas': 1, 'availableReplicas': 1}}
        for changed in ({'observedGeneration': 2}, {'updatedReplicas': 0},
                        {'replicas': 2}, {'availableReplicas': 0}):
            with self.subTest(changed=changed):
                d = json.loads(json.dumps(base))
                d['status'].update(changed)
                self.assertFalse(s.single_replica_rollout_complete(d))
        self.assertFalse(s.single_replica_rollout_complete({}))
        base['spec']['replicas'] = 2
        self.assertFalse(s.single_replica_rollout_complete(base))


if __name__ == '__main__':
    unittest.main(verbosity=2)
