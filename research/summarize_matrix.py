"""Validate matched matrix blocks and summarize seeds within each story."""
import argparse
import json
from pathlib import Path
import statistics
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from artifacts import write_json
from research.compare_post import compare


def summarize(root, decode=False):
    root=Path(root);plan=json.loads((root/'plan.json').read_text())
    selection=json.loads((root/'matrix_selection.json').read_text())
    result=[];paired=[];groups={}
    for block in plan['blocks']:
        if selection['stories'] and block['story'] not in selection['stories']:continue
        if selection['seeds'] and block['seed'] not in selection['seeds']:continue
        methods=[m for m in block['method_order'] if not selection['methods'] or m in selection['methods']]
        comparison=compare([root/block['id']/m for m in methods],decode=decode)
        result.append(dict(block=block['id'],story=block['story'],seed=block['seed'],comparison=comparison))
        rows={r['method']:r for r in comparison['rows']}
        if 'attr_dag' in rows and 'dair' in rows:
            paired.append(dict(block=block['id'],dair_minus_attr_video_intents=rows['dair']['edit_submission_intents']-rows['attr_dag']['edit_submission_intents']))
        for row in comparison['rows']:groups.setdefault((block['story'],row['method']),[]).append(row)
    story_rows=[]
    for (story,method),rows in groups.items():
        times=[r['edit_completion_observed_seconds'] for r in rows]
        story_rows.append(dict(story=story,method=method,seed_runs=len(rows),
            edit_video_intents=[r['edit_submission_intents'] for r in rows],
            client_seconds_mean=statistics.mean(times),
            client_seconds_sample_sd=statistics.stdev(times) if len(times)>1 else None))
    return dict(blocks=result,story_summary=story_rows,paired_dair_attr=paired,
                independent_story_count=len({r['story'] for r in result}),
                gpu_seconds=None,semantic_quality=None,
                interpretation='Seeds are repetitions within author-designed stories. No claim of 45 independent stories. '
                'Inspect visual fidelity separately. Attribute-aware baseline ties must be reported. Client time is not GPU time.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');p.add_argument('--output',required=True);p.add_argument('--decode',action='store_true')
    a=p.parse_args();r=summarize(a.root,a.decode);write_json(a.output,r)
    print(json.dumps(dict(stories=r['independent_story_count'],blocks=len(r['blocks']),paired_dair_attr=r['paired_dair_attr']),indent=2))
