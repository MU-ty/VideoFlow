"""Archive immutable revision manifests, content hashes and optional media probes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib import parse, request

from asset_runtime import Conflict, fingerprint


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def trusted_download(url, backend, output, max_bytes, timeout):
    origin, target = parse.urlsplit(backend), parse.urlsplit(url)
    if (origin.scheme not in ('http','https') or target.scheme != origin.scheme or
            target.netloc != origin.netloc or target.path != origin.path.rstrip('/')+'/view' or
            target.fragment or target.username or target.password):
        raise ValueError('archive accepts only /view URLs from the configured backend')
    # Refuse redirects rather than following a provider URL to another origin.
    opener = request.build_opener(NoRedirect())
    total = 0
    with opener.open(url, timeout=timeout) as response, Path(output).open('wb') as f:
        declared = response.headers.get('Content-Length')
        if declared is not None and int(declared) > max_bytes:
            raise ValueError('artifact exceeds download limit')
        for block in iter(lambda: response.read(1024*1024), b''):
            total += len(block)
            if total > max_bytes:
                raise ValueError('artifact exceeds download limit')
            f.write(block)
        if not total or (declared is not None and total != int(declared)):
            raise ValueError('empty or incomplete artifact download')
        f.flush(); os.fsync(f.fileno())


def probe_media(path, executable='ffprobe'):
    result = subprocess.run([executable,'-v','error','-show_format','-show_streams','-of','json',str(path)],
                            check=True, capture_output=True, text=True, timeout=60)
    probe = json.loads(result.stdout)
    if not any(s.get('codec_type')=='video' for s in probe.get('streams', [])):
        raise ValueError('ffprobe found no video stream')
    return probe


def archive_current(runtime, directory, backend, *, max_bytes=2*1024**3,
                    timeout=60, probe=False, downloader=trusted_download):
    """Returns an archive manifest; input provenance and downloaded content differ."""
    if max_bytes <= 0 or timeout <= 0:
        raise ValueError('positive download bounds required')
    snapshot = runtime.snapshot()
    timeline = snapshot['timeline']
    if timeline is None:
        raise Conflict('current timeline is incomplete')
    root = Path(directory).resolve(); root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / ('revision-'+str(snapshot['revision'])+'.json')
    identity = fingerprint(timeline)
    cached = {}
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding='utf-8'))
        if old.get('timeline_hash') != identity:
            raise Conflict('archive path already belongs to different inputs')
        cached = {entry['asset']:entry for entry in old['assets']}
    # Reused assets can retain verified bytes archived for an older revision.
    for previous in sorted(root.glob('revision-*.json')):
        old = json.loads(previous.read_text(encoding='utf-8'))
        for entry in old.get('assets', []):
            matching = next((a for a in timeline if a['asset']==entry['asset'] and
                             a['input_hash']==entry['input_hash'] and a['output']==entry['output']), None)
            if matching is not None and entry['asset'] not in cached:
                cached[entry['asset']] = entry
    entries = []
    jobs = {(j['asset'], j['revision']):j for j in snapshot['jobs']}
    for item in timeline:
        prior = cached.get(item['asset'])
        if prior is not None and any(prior.get(k)!=v for k,v in item.items()):
            prior = None
        local = None if prior is None else root / prior['file']
        if local is not None and (not local.resolve().is_relative_to(root)):
            raise ValueError('archive file escapes output directory')
        if local is not None and local.is_file() and local.stat().st_size <= max_bytes and checksum(local)==prior['sha256']:
            entry = dict(prior)
        else:
            suffix = '.txt' if item['kind']=='subtitle' else '.mp4'
            if item['kind']=='video':
                filename = parse.parse_qs(parse.urlsplit(item['output']).query).get('filename',[''])[0]
                suffix = Path(filename).suffix.lower()
                if suffix not in ('.mp4','.webm','.mov'):
                    raise ValueError('unrecognized video extension')
            fd, temporary = tempfile.mkstemp(prefix='.artifact-', dir=root)
            os.close(fd)
            try:
                if item['kind']=='subtitle':
                    data = item['output'].encode('utf-8')
                    if len(data)>max_bytes:
                        raise ValueError('subtitle exceeds size limit')
                    Path(temporary).write_bytes(data)
                else:
                    if jobs[(item['asset'],item['revision'])]['backend'] != backend:
                        raise Conflict('artifact belongs to a different backend')
                    downloader(item['output'],backend,temporary,max_bytes,timeout)
                size = Path(temporary).stat().st_size
                if not size or size>max_bytes:
                    raise ValueError('invalid artifact size')
                digest = checksum(temporary)
                name = fingerprint(item['asset'])[:12]+'-'+item['input_hash']+'-'+digest+suffix
                os.replace(temporary, root/name)
                entry = dict(item, file=name, sha256=digest, bytes=size, media_probe=None)
            finally:
                if Path(temporary).exists():
                    Path(temporary).unlink()
        if probe and item['kind']=='video':
            entry['media_probe'] = probe_media(root/entry['file'])
        entries.append(entry)
    current = runtime.snapshot()
    if current['revision']!=snapshot['revision'] or fingerprint(current['timeline'])!=identity:
        raise Conflict('project changed during archival; retry current revision')
    manifest = dict(revision=snapshot['revision'], timeline_hash=identity, assets=entries,
                    content_hash_note='sha256 identifies local bytes; input_hash identifies generation provenance')
    write_json(manifest_path,manifest)
    return manifest
