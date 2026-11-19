"""Generate and freeze Rev.1 videos for the active verification holdout."""
import argparse
import csv
from pathlib import Path
import subprocess
import sys

from active_budget import SCREEN_FIELDS, load, save

NATURAL = Path(__file__).resolve().parent.parent / 'VideoFlow_Natural_Gate'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:8188')
    parser.add_argument('--ollama', default='http://127.0.0.1:11434')
    parser.add_argument('--timeout', default=2400, type=float)
    parser.add_argument('--poll', default=2, type=float)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    sys.path.insert(0, str(NATURAL))
    from run_joint import live_setup, run_seed

    protocol = load(args.protocol)
    if not protocol.get('requires_screen') or args.timeout <= 0 or args.poll <= 0:
        parser.error('requires a fresh holdout protocol and positive timeout/poll')
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    locked = root / 'protocol.json'
    if locked.exists() and load(locked) != protocol:
        raise ValueError('source run uses a different protocol')
    save(locked, protocol)
    args.repo = NATURAL / 'runtime'
    args.policy = []
    args.reuse_source_from = None
    report_path = root / 'source-report.json'
    report = load(report_path) if report_path.exists() else {}
    if not args.prepare_only and set(report) != set(protocol['cases']):
        report = {block: report.get(block, {'status': 'pending'}) for block in protocol['cases']}
        save(report_path, report)

    for index, (block, case) in enumerate(sorted(protocol['cases'].items())):
        if report.get(block, {}).get('status') in ('completed', 'failed'):
            continue
        args.output = root / block
        if args.output.exists():
            raise ValueError(f'partial source block already exists: {args.output}')
        try:
            run_seed(args, case, index, policies=[])
            if args.prepare_only:
                continue
            api = live_setup(args.repo)
            source = args.output / 'source_media'
            archive = load(source / 'revision-1.json')
            screens = {}
            for asset in archive['assets']:
                shot = asset['asset']
                if shot not in ('S1', *case['candidates']):
                    continue
                path = args.output / 'frozen' / f'{shot}.png'
                if path.exists():
                    screened = load(args.output / 'frozen' / 'index.json')[shot]
                else:
                    screened = api['make_sheet'](source / asset['file'], path)
                if screened['clip_sha256'] != asset['sha256']:
                    raise ValueError(f'screen uses a different clip: {block}/{shot}')
                screens[shot] = screened
            report[block] = dict(status='completed', screening_inputs=screens)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            if args.prepare_only:
                raise
            report[block] = dict(status='failed', error=str(exc))
            print(f'{block}: {exc}', flush=True)
        save(report_path, report)

    if args.prepare_only:
        return
    template = root / 'source-screen.csv'
    if not template.exists():
        with template.open('w', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=SCREEN_FIELDS)
            writer.writeheader()
            for block, row in sorted(report.items()):
                if row['status'] == 'completed':
                    for shot in ('S1', *protocol['cases'][block]['candidates']):
                        writer.writerow(dict(block=block, shot=shot))


if __name__ == '__main__':
    main()
