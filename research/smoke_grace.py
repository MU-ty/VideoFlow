from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
import json

import recovery_observability_comfy as experiment


class Clock:
    value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Provider:
    def __init__(self, clock):
        self.clock = clock
        self.jobs = {}
        self.base = 'http://simulation.invalid'
        self.timeout = 30
        self.last_inspection = None

    def environment(self):
        return {'simulated': True}

    def _request(self, path):
        assert path == '/queue'
        return dict(queue_pending=[], queue_running=[pid for pid, due in self.jobs.items() if self.clock.now() < due])

    def submit(self, workflow, prompt_id):
        assert prompt_id not in self.jobs
        self.jobs[prompt_id] = self.clock.now() + 21
        return prompt_id

    def inspect(self, prompt_id):
        self.last_inspection = None
        if prompt_id not in self.jobs:
            return 'unknown', None
        if self.clock.now() < self.jobs[prompt_id]:
            return 'running', None
        return 'succeeded', self.base + '/' + prompt_id + '.mp4'


def main():
    with tempfile.TemporaryDirectory() as tmp:
        for policy in ('fixed_grace15', 'backoff_after_3', 'replace_after_3'):
            for fault in ('transient_wall', 'opaque'):
                clock = Clock()
                provider = Provider(clock)
                root = Path(tmp) / (policy + '-' + fault)
                with patch.object(time, 'monotonic', clock.now), patch.object(time, 'sleep', clock.sleep), \
                        patch.object(time, 'time', lambda: 1_800_000_000 + clock.now()), \
                        patch.object(experiment, 'ComfyUI', lambda *_: provider):
                    result = experiment.run_trial(root, provider, policy, fault, 5, 180, 0)
                assert result['correct_publication_seconds'] is not None
                assert not result['stale_publications']
                assert result['original_s6_reused']
                replacement = result['replacements'].get('S4')
                if fault == 'opaque' or policy == 'replace_after_3':
                    assert replacement['queued_after_seconds'] == (10 if policy == 'replace_after_3' else 15)
                    assert result['accepted_count'] == 5
                else:
                    assert not replacement
                    assert result['accepted_count'] == 4
                if policy == 'fixed_grace15':
                    polls = [json.loads(line) for line in (root / 'worker.jsonl').read_text().splitlines()]
                    assert [p['probe_started'] for p in polls if p['target_probed']][:4] == [0, 5, 10, 15]
    print('Six deterministic integration cases passed; no GPU involved.')


if __name__ == "__main__":
    main()
