"""Synthetic provider for execution probes, never generates playable video."""
import hashlib
from pathlib import Path
from urllib import parse


class MockProvider:
    base = 'http://mock.invalid'

    def __init__(self):
        self.jobs = {}
        self.posts = []
        self.downloads = 0
        self.ready = True

    def submit(self, graph, pid):
        self.posts.append(pid)
        # Stable synthetic bytes per prompt, not a model output or valid MP4.
        prompt = graph['5']['inputs']['prompt']
        self.jobs[pid] = b'SYNTHETIC-NOT-A-VIDEO\n'+prompt.encode()
        return pid

    def inspect(self, pid):
        if pid not in self.jobs:
            return 'unknown', None
        if not self.ready:
            return 'running', None
        return 'succeeded', self.base+'/view?'+parse.urlencode({'filename':pid+'.mp4','type':'output'})

    def download(self, url, backend, output, max_bytes, timeout):
        self.downloads += 1
        pid = parse.parse_qs(parse.urlsplit(url).query)['filename'][0][:-4]
        data = self.jobs[pid]
        if len(data)>max_bytes:
            raise ValueError('mock artifact exceeds limit')
        Path(output).write_bytes(data)
