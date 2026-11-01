import argparse, json
from videoflow import Runtime
p=argparse.ArgumentParser(); p.add_argument('--db',default='demo.db'); args=p.parse_args()
r=Runtime(args.db)
for s in ('S1','S2','S3'): r.add_scene(s,'Storyboard '+s)
r.depend('S1','S2')
while ready := r.ready():
    for t in ready:
        ext='fake-'+t['id']; r.submit(t['id'],ext); r.complete(t['id'],ext,'fake://'+t['scene']+'.mp4')
print('Affected:',r.revise('S1','Updated storyboard'))
print(json.dumps(r.snapshot(),indent=2))
