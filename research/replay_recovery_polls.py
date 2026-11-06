"""Compare replacement trigger decisions under a shared visibility mask."""
import json


def replay(mask_end, gaps):
    probes = [0]
    for gap in gaps:
        probes.append(probes[-1] + gap)
    for index, probe in enumerate(probes):
        if mask_end is not None and probe >= mask_end:
            return dict(probes_seconds=probes[:index + 1], first_visible_seconds=probe,
                        replace_trigger_seconds=None)
    return dict(probes_seconds=probes, first_visible_seconds=None,
                replace_trigger_seconds=probes[-1])


if __name__ == '__main__':
    print(json.dumps({
        'evidence': 'deterministic_polling_replay_only',
        'unknown_threshold': 3,
        'rows': [
            dict(mask_end_seconds=mask_end, fixed=replay(mask_end, (5, 5)),
                 backoff=replay(mask_end, (5, 10)))
            for mask_end in (0, 7, 12, 20, None)
        ],
    }, indent=2))
