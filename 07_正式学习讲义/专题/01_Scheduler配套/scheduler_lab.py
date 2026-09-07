#!/usr/bin/env python3
"""Scheduler study labs. Python 3.9+, stdlib only. Never uses the default kubeconfig.

Creates ONLY its own kind cluster; no production context option is provided.
Cluster integration is not claimed tested by this teaching artifact.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

LABEL = 'scheduler-study.example/owner'
TARGET = 'scheduler-study.example/target'
BLOCK = 'scheduler-study.example/block'
NS = 'scheduler-study-lab'
CASES = ('cpu', 'affinity', 'binding', 'gates', 'rollout', 'preemption')
IMAGE = 'docker.io/library/busybox:1.37.0'


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def cpu_m(value: str) -> int:
    """CPU quantity -> millicores, rounded up; sufficient for lab Node capacity."""
    multipliers = {'n': Decimal('0.000001'), 'u': Decimal('0.001'), 'm': Decimal('1')}
    suffix = value[-1:]
    n = Decimal(value[:-1]) * multipliers[suffix] if suffix in multipliers else Decimal(value) * 1000
    if not n.is_finite() or n < 0:
        raise ValueError('Invalid CPU quantity')
    return math.ceil(n)


def shape_cpu(allocatable: int) -> int:
    """One needs to fit; two must exceed allocatable. Existing usage is NOT inferred."""
    if allocatable < 1000:
        raise ValueError('Lab worker needs at least 1 allocatable CPU')
    return allocatable * 55 // 100 + 1


def command(argv: List[str], data: Optional[str] = None, timeout: int = 60) -> str:
    try:
        p = subprocess.run(argv, input=data, text=True, encoding='utf-8',
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'Command failed: {argv[0]}: {exc}') from exc
    if p.returncode:
        raise RuntimeError(f'Exit {p.returncode}: {" ".join(argv)}\n{p.stderr[-4000:]}')
    return p.stdout


def pod(name: str, owner: str, cpu: str = '50m', image: str = IMAGE) -> Dict[str, Any]:
    return {'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': name, 'namespace': NS, 'labels': {LABEL: owner, 'app': 'study'}},
            'spec': {'nodeSelector': {TARGET: owner}, 'terminationGracePeriodSeconds': 20,
                     'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                     'containers': [{'name': 'sleeper', 'image': image,
                                     'imagePullPolicy': 'IfNotPresent',
                                     'command': ['sh', '-c', 'sleep 3600'],
                                     'resources': {'requests': {'cpu': cpu, 'memory': '64Mi'},
                                                   'limits': {'memory': '128Mi'}}}]}}


def is_bound(p: Dict[str, Any]) -> bool:
    return bool(p.get('spec', {}).get('nodeName'))


def has_condition(p: Dict[str, Any], kind: str, status: str) -> bool:
    return any(c.get('type') == kind and c.get('status') == status
               for c in p.get('status', {}).get('conditions', []))


def unschedulable(p: Dict[str, Any]) -> bool:
    return not is_bound(p) and any(c.get('type') == 'PodScheduled'
        and c.get('status') == 'False' and c.get('reason') == 'Unschedulable'
        for c in p.get('status', {}).get('conditions', []))


def single_replica_rollout_complete(d: Dict[str, Any]) -> bool:
    """Lab-only completion check; an old available replica is insufficient."""
    generation = d.get('metadata', {}).get('generation', 0)
    status = d.get('status', {})
    return (generation > 0
            and d.get('spec', {}).get('replicas') == 1
            and status.get('observedGeneration', 0) >= generation
            and status.get('updatedReplicas', 0) == 1
            and status.get('replicas', 0) == 1
            and status.get('availableReplicas', 0) == 1)


class Lab:
    def __init__(self, directory: Path):
        self.root = directory.resolve()
        self.state_path = self.root / 'state.json'
        self.kubeconfig = self.root / 'kubeconfig'
        self.state: Dict[str, Any] = {}
        self.evidence: Optional[Path] = None

    def k(self, *args: str, data: Optional[str] = None, timeout: int = 60) -> str:
        name = self.state.get('name', '')
        if not re.fullmatch(r'scheduler-study-[0-9a-f]{8}', name):
            raise RuntimeError('Refusing an unrecognized cluster name')
        return command(['kubectl', '--kubeconfig', str(self.kubeconfig),
                        '--context', 'kind-' + name, '--request-timeout=20s', *args], data, timeout)

    def obj(self, *args: str) -> Dict[str, Any]:
        text = self.k('get', *args, '-o', 'json', '--ignore-not-found')
        return json.loads(text) if text.strip() else {}

    def verify(self) -> None:
        if not self.state_path.is_file() or not self.kubeconfig.is_file():
            raise RuntimeError('No owned lab state/kubeconfig. Run init first.')
        self.state = json.loads(self.state_path.read_text(encoding='utf-8'))
        if self.state.get('destroyed'):
            raise RuntimeError('This lab was destroyed. Use a new --state directory.')
        names = command(['kind', 'get', 'clusters']).splitlines()
        if self.state.get('name') not in names:
            raise RuntimeError('Owned kind cluster not present. Refusing any Kubernetes mutation.')
        uid = self.obj('namespace', 'kube-system').get('metadata', {}).get('uid')
        if not uid or uid != self.state.get('cluster_uid'):
            raise RuntimeError('Cluster identity changed. Refusing any Kubernetes mutation.')
        if not self.obj('node', self.state['node']):
            raise RuntimeError('Owned worker is missing. Refusing mutations.')

    def init(self, node_image: str, image: str) -> None:
        if self.root.exists() and any(self.root.iterdir()):
            raise RuntimeError('State directory is not empty; use a NEW directory.')
        if not re.fullmatch(r'kindest/node:v\d+\.\d+\.\d+(?:@sha256:[0-9a-f]{64})?', node_image):
            raise RuntimeError('Choose a versioned kindest/node image from your kind release notes.')
        for binary in ('kind', 'kubectl'):
            if not shutil.which(binary):
                raise RuntimeError(f'{binary} is missing from PATH')
        self.root.mkdir(parents=True, exist_ok=True)
        name = 'scheduler-study-' + uuid.uuid4().hex[:8]
        self.state = {'name': name, 'owner': uuid.uuid4().hex, 'created': now(),
                      'node_image': node_image, 'image': image, 'phase': 'creating'}
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding='utf-8')
        config = self.root / 'kind.yaml'
        config.write_text('kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\nnodes:\n'
                          '- role: control-plane\n- role: worker\n- role: worker\n', encoding='utf-8')
        print(f'Creating owned local cluster: {name}', flush=True)
        command(['kind', 'create', 'cluster', '--name', name, '--image', node_image,
                 '--config', str(config), '--kubeconfig', str(self.kubeconfig), '--wait', '180s'], timeout=600)
        uid = self.obj('namespace', 'kube-system')['metadata']['uid']
        nodes = self.obj('nodes')['items']
        workers = [n for n in nodes if 'node-role.kubernetes.io/control-plane' not in n['metadata'].get('labels', {})]
        if not workers:
            raise RuntimeError('No worker was created; inspect the named local cluster.')
        node = sorted(workers, key=lambda n: n['metadata']['name'])[0]['metadata']['name']
        self.state.update(cluster_uid=uid, node=node, phase='active')
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding='utf-8')
        if os.name != 'nt':
            self.kubeconfig.chmod(0o600)
        self.k('label', 'node', node, f'{TARGET}={self.state["owner"]}')
        self.k('wait', '--for=condition=Ready', 'node', node, '--timeout=180s', timeout=200)
        (self.root / 'version.json').write_text(self.k('version', '-o', 'json'), encoding='utf-8')
        print(f'Created {name}. Dedicated kubeconfig: {self.kubeconfig}\nDefault context was not modified.')

    def apply(self, value: Dict[str, Any]) -> None:
        payload = json.dumps(value)
        if self.evidence:
            filename = f'{value["kind"]}-{value["metadata"]["name"]}.json'
            (self.evidence / filename).write_text(payload, encoding='utf-8')
        self.k('apply', '-f', '-', '--dry-run=server', data=payload)
        self.k('apply', '-f', '-', data=payload)

    def snapshot(self, label: str) -> None:
        if self.evidence is None:
            return
        record: Dict[str, Any] = {'time': now(), 'stage': label}
        for key, args in [('pods', ('pods', '-n', NS)), ('events', ('events', '-n', NS)),
                          ('deployments', ('deployments', '-n', NS)), ('node', ('node', self.state['node'])),
                          ('node_pods', ('pods', '-A', '--field-selector',
                                         'spec.nodeName=' + self.state['node']))]:
            try:
                record[key] = self.obj(*args)
            except RuntimeError as exc:
                record[key] = {'collection_error': str(exc)}
        with (self.evidence / 'timeline.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    def wait(self, description: str, predicate: Callable[[], bool], seconds: int = 180) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                self.snapshot(description + ': passed')
                print('PASS:', description, flush=True)
                return
            self.snapshot(description + ': waiting')
            time.sleep(2)
        raise RuntimeError(f'Timeout: {description}. Inspect evidence; this is NOT a passing experiment.')

    def p(self, name: str) -> Dict[str, Any]:
        return self.obj('pod', name, '-n', NS)

    def new_pod(self, name: str, cpu: str = '50m', **spec: Any) -> Dict[str, Any]:
        value = pod(name, self.state['owner'], cpu, self.state['image'])
        value['spec'].update(spec)
        return value

    def clean(self) -> None:
        self.verify()
        ns = self.obj('namespace', NS)
        if ns and ns['metadata'].get('labels', {}).get(LABEL) != self.state['owner']:
            raise RuntimeError('Namespace ownership mismatch. Refusing deletion.')
        self.k('delete', 'namespace', NS, '--ignore-not-found', '--wait=true', '--timeout=120s', timeout=150)
        self.k('delete', 'priorityclass', '-l', f'{LABEL}={self.state["owner"]}', '--ignore-not-found')
        taints = self.obj('node', self.state['node']).get('spec', {}).get('taints', [])
        if any(t.get('key') == BLOCK for t in taints):
            self.k('taint', 'node', self.state['node'], BLOCK + ':NoSchedule-')
        print('Lab namespace, owned PriorityClasses and test taint cleaned. Evidence retained.')

    def run(self, case: str) -> None:
        self.verify()
        if self.obj('namespace', NS):
            raise RuntimeError('Previous lab exists. Run clean before another case.')
        node = self.obj('node', self.state['node'])
        if any(t.get('key') == BLOCK for t in node.get('spec', {}).get('taints', [])):
            raise RuntimeError('Previous test taint exists. Run clean first.')
        alloc = cpu_m(node['status']['allocatable']['cpu'])
        request = str(shape_cpu(alloc)) + 'm'
        tag = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.evidence = self.root / 'evidence' / f'{tag}-{case}-{uuid.uuid4().hex[:4]}'
        self.evidence.mkdir(parents=True)
        self.apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': NS, 'labels': {LABEL: self.state['owner']}}})
        print(f'CASE {case}; node={self.state["node"]}; allocatable={alloc}m; shape={request}')
        try:
            if case == 'cpu':
                self.apply(self.new_pod('too-large', str(alloc + 1000) + 'm'))
                self.wait('oversized Pod rejected before binding', lambda: unschedulable(self.p('too-large')))
                self.apply(self.new_pod('fits'))
                self.wait('same placement, small request binds', lambda: is_bound(self.p('fits')))
            elif case == 'affinity':
                self.k('taint', 'node', self.state['node'], BLOCK + '=yes:NoSchedule')
                self.apply(self.new_pod('no-toleration'))
                self.wait('selector alone cannot bypass taint', lambda: unschedulable(self.p('no-toleration')))
                self.apply(self.new_pod('with-toleration', tolerations=[{'key': BLOCK, 'operator': 'Equal', 'value': 'yes', 'effect': 'NoSchedule'}]))
                self.wait('selector plus toleration binds', lambda: is_bound(self.p('with-toleration')))
            elif case == 'binding':
                bad = self.new_pod('bad-image')
                bad['spec']['containers'][0]['image'] = 'registry.invalid/not-a-real-image:0'
                self.apply(bad)
                self.wait('bad-image Pod still gets a node', lambda: is_bound(self.p('bad-image')))
                self.wait('node-side image failure after binding', lambda: any(
                    c.get('state', {}).get('waiting', {}).get('reason') in ('ErrImagePull', 'ImagePullBackOff')
                    for c in self.p('bad-image').get('status', {}).get('containerStatuses', [])))
            elif case == 'gates':
                self.apply(self.new_pod('gated', schedulingGates=[{'name': 'scheduler-study.example/release'}]))
                time.sleep(4)
                p = self.p('gated')
                if is_bound(p) or not p.get('spec', {}).get('schedulingGates'):
                    raise RuntimeError('Gate did not keep Pod unbound')
                self.snapshot('gate present: unbound')
                self.k('patch', 'pod', 'gated', '-n', NS, '--type=json', '-p', '[{"op":"remove","path":"/spec/schedulingGates"}]')
                self.wait('removing gate permits binding', lambda: is_bound(self.p('gated')))
            elif case == 'rollout':
                template = self.new_pod('template', request)
                template['spec']['restartPolicy'] = 'Always'
                deployment = {'apiVersion': 'apps/v1', 'kind': 'Deployment',
                    'metadata': {'name': 'rollout-demo', 'namespace': NS, 'labels': {LABEL: self.state['owner']}},
                    'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': 'study'}},
                        'strategy': {'type': 'RollingUpdate', 'rollingUpdate': {'maxSurge': 1, 'maxUnavailable': 0}},
                        'template': {'metadata': {'labels': template['metadata']['labels']}, 'spec': template['spec']}}}
                self.apply(deployment)
                self.k('rollout', 'status', 'deployment/rollout-demo', '-n', NS, '--timeout=180s', timeout=210)
                self.snapshot('old replica ready')
                patch = {'spec': {'template': {'metadata': {'annotations': {'study-revision': uuid.uuid4().hex}}}}}
                self.k('patch', 'deployment', 'rollout-demo', '-n', NS, '--type=merge', '-p', json.dumps(patch))
                self.wait('surge creates unbound Pod while old replica is Ready', lambda:
                    any(unschedulable(p) for p in self.obj('pods', '-n', NS).get('items', [])) and
                    any(has_condition(p, 'Ready', 'True') for p in self.obj('pods', '-n', NS).get('items', [])))
                print('LAB ONLY: allowing one unavailable replica demonstrates a downtime tradeoff.')
                patch = {'spec': {'strategy': {'rollingUpdate': {'maxSurge': 0, 'maxUnavailable': 1}}}}
                self.k('patch', 'deployment', 'rollout-demo', '-n', NS, '--type=merge', '-p', json.dumps(patch))
                self.wait('replacement rollout completes after budget change', lambda:
                    single_replica_rollout_complete(
                        self.obj('deployment', 'rollout-demo', '-n', NS)))
                self.k('rollout', 'status', 'deployment/rollout-demo', '-n', NS, '--timeout=180s', timeout=210)
            elif case == 'preemption':
                names = {}
                for role, value, policy in [('low', 100, 'PreemptLowerPriority'), ('never', 20000, 'Never'), ('high', 30000, 'PreemptLowerPriority')]:
                    names[role] = 'ss-' + role + '-' + self.state['owner'][:8]
                    self.apply({'apiVersion': 'scheduling.k8s.io/v1', 'kind': 'PriorityClass',
                        'metadata': {'name': names[role], 'labels': {LABEL: self.state['owner']}},
                        'value': value, 'globalDefault': False, 'preemptionPolicy': policy})
                low = self.new_pod('low', request, priorityClassName=names['low'])
                low['spec']['containers'][0]['lifecycle'] = {'preStop': {'exec': {'command': ['sh', '-c', 'sleep 5']}}}
                self.apply(low)
                self.wait('low-priority Pod occupies node BEFORE competitors', lambda: has_condition(self.p('low'), 'Ready', 'True'))
                self.apply(self.new_pod('never', request, priorityClassName=names['never']))
                self.wait('non-preempting high priority remains unschedulable', lambda: unschedulable(self.p('never')))
                time.sleep(4)
                if self.p('low').get('metadata', {}).get('deletionTimestamp') or not has_condition(self.p('low'), 'Ready', 'True'):
                    raise RuntimeError('Low Pod did not survive Never control')
                self.snapshot('Never control: low survives')
                self.k('delete', 'pod', 'never', '-n', NS, '--wait=true')
                self.apply(self.new_pod('high', request, priorityClassName=names['high']))
                self.wait('preempting high priority eventually binds', lambda: is_bound(self.p('high')))
                self.wait('low-priority victim is removed', lambda: not self.p('low'))
            else:
                raise ValueError('Unknown case')
            self.snapshot('complete')
            print('Completed assertions. Inspect timeline.jsonl; write your explanation, then run clean.')
        finally:
            self.snapshot('final state (also recorded on failure)')
            print('Evidence:', self.evidence)
            print('Objects are retained for inspection. Do NOT use these changes on production.')

    def destroy(self, confirmation: str) -> None:
        self.verify()
        if confirmation != self.state['name']:
            raise RuntimeError('Use --confirm followed by the exact owned cluster name in state.json')
        command(['kind', 'delete', 'cluster', '--name', self.state['name'], '--kubeconfig', str(self.kubeconfig)], timeout=180)
        self.state['destroyed'] = now()
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding='utf-8')
        print('Owned kind cluster deleted; evidence and state retained.')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path('.scheduler-study'), help='Dedicated local state directory')
    sub = parser.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init', help='Create a NEW owned local kind cluster')
    init.add_argument('--node-image', required=True, help='Versioned kindest/node image compatible with your kind release')
    init.add_argument('--image', default=IMAGE, help='Approved BusyBox-compatible image including sh and sleep')
    run = sub.add_parser('run', help='Run one lab; objects remain until clean')
    run.add_argument('case', choices=CASES)
    sub.add_parser('clean', help='Delete only this lab namespace, PriorityClasses and test taint')
    destroy = sub.add_parser('destroy', help='Delete the entire owned local cluster')
    destroy.add_argument('--confirm', required=True)
    args = parser.parse_args()
    lab = Lab(args.state)
    try:
        if args.action == 'init': lab.init(args.node_image, args.image)
        elif args.action == 'run': lab.run(args.case)
        elif args.action == 'clean': lab.clean()
        else: lab.destroy(args.confirm)
        return 0
    except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f'ERROR: {exc}\nNo success is claimed. Preserve evidence before cleanup.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted. Owned lab resources may remain; inspect state before clean.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
