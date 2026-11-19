"""Mirror the candidate IDs in a recorded shared-response bank."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

from active_budget import BUDGETS, FRACTIONS, load, observations, save, score

SWAP = {'S2': 'S3', 'S3': 'S2'}


def mirror(case):
    def renamed(shot):
        return SWAP.get(shot, shot)

    return dict(case, expected=[renamed(shot) for shot in case['expected']],
                edges=[[renamed(parent), renamed(child)] for parent, child in case['edges']])


def replay_archive(archive, protocol_path):
    protocol = load(protocol_path)
    with ZipFile(archive) as z:
        report_path, = [name for name in z.namelist() if name.count('/') <= 1
                        and name.rsplit('/', 1)[-1] == 'report.json']
        root = report_path[:-len('report.json')]
        report = json.loads(z.read(report_path))
        metadata = json.loads(z.read(root + 'metadata.json'))
        if (metadata['protocol_sha256'] != sha256(protocol_path.read_bytes()).hexdigest()
                or metadata['model']['model'] != protocol['model']):
            raise ValueError('archive and protocol differ')
        if report['design'] != 'shared-response-bank-replay':
            raise ValueError('requires a shared response bank')
        cases = {}
        banks = {}
        for block, eligible in sorted(report['source_eligible']['seeds'].items()):
            original = protocol['cases'][block]
            # Recover the original tie order and keep it fixed by position in both arms.
            order = [shot for shot, _ in eligible[str(BUDGETS[0])]['fixed']['trace'][:2]]
            if len(order) != 2 or set(order) != set(SWAP):
                raise ValueError(f'invalid candidate order: {block}')
            case = dict(original, recheck_order=order)
            bank = {}
            for shot in order:
                rows = []
                for view in range(1, len(FRACTIONS) + 1):
                    stem = f'{root}{block}/{shot}-{view}'
                    row = json.loads(z.read(stem + '.json'))
                    parsed = ['unknown', 'unknown'] if row.get('error') else observations(
                        row['raw'], protocol['schema'], case)
                    if (row['prompt'] != case['prompt'] or row['votes'] != parsed
                            or row['image_sha256'] != sha256(z.read(stem + '.png')).hexdigest()):
                        raise ValueError(f'mismatched archived VLM input or reply: {stem}')
                    rows.append(row)
                bank[shot] = rows
            cases[block] = case
            banks[block] = bank
    original = score(cases, banks, BUDGETS)
    if json.loads(json.dumps(original)) != report['source_eligible']:
        raise ValueError('original scores do not match the archive')
    mirrored_cases = {block: mirror(case) for block, case in cases.items()}
    mirrored_banks = {block: {SWAP[shot]: rows for shot, rows in bank.items()}
                      for block, bank in banks.items()}
    paired_cases = {f'{block}/original': case for block, case in cases.items()}
    paired_cases.update({f'{block}/mirrored': case for block, case in mirrored_cases.items()})
    paired_banks = {f'{block}/original': bank for block, bank in banks.items()}
    paired_banks.update({f'{block}/mirrored': bank for block, bank in mirrored_banks.items()})
    return dict(design='paired-id-relabeling-of-existing-evidence',
                new_vlm_calls=0, unique_seeds=len(cases),
                original=original,
                mirrored=score(mirrored_cases, mirrored_banks, BUDGETS),
                paired=score(paired_cases, paired_banks, BUDGETS))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = replay_archive(args.archive, args.protocol)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save(args.output, result)
    print(args.output)


if __name__ == '__main__':
    main()
