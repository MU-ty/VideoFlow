"""MiniMax H3 workflow and ComfyUI HTTP adapter (standard library only)."""
import copy
import json
from urllib import request, parse, error

class BackendError(Exception): pass
class SubmissionUncertain(BackendError): pass
class SubmissionRejected(BackendError): pass

class ComfyUI:
    def __init__(self, base, timeout=30):
        self.base=base.rstrip('/')
        if not self.base.startswith(('http://','https://')): raise ValueError('server must be an HTTP(S) URL')
        self.timeout=timeout
        self.last_inspection=None
    def _request(self, path, data=None):
        body=None if data is None else json.dumps(data).encode()
        req=request.Request(self.base+path, data=body, headers={'Content-Type':'application/json'})
        with request.urlopen(req, timeout=self.timeout) as res: return json.load(res)
    def environment(self):
        return self._request('/system_stats')

    def submit(self, workflow, prompt_id):
        try:
            result=self._request('/prompt',{'prompt':workflow,'prompt_id':prompt_id,'client_id':prompt_id})
        except error.HTTPError as exc:
            # 4xx validation rejection is definitive; 5xx may have accepted the job.
            detail=exc.read(4000).decode('utf-8','replace')
            if 400 <= exc.code < 500: raise SubmissionRejected(f'HTTP {exc.code}: {detail}') from exc
            raise SubmissionUncertain(f'HTTP {exc.code}; inspect job {prompt_id}') from exc
        except (OSError, ValueError) as exc:
            raise SubmissionUncertain(f'Submission response unknown; inspect job {prompt_id}') from exc
        if result.get('prompt_id') != prompt_id:
            raise SubmissionUncertain(f'Unexpected prompt response; inspect job {prompt_id}: {result}')
        return prompt_id
    def inspect(self, prompt_id):
        self.last_inspection=None
        pid=parse.quote(prompt_id,safe='')
        history=self._request('/history/'+pid)
        entry=history.get(prompt_id)
        self.last_inspection={'prompt_id':prompt_id,'history_entry':entry,'queue':None}
        if entry is not None:
            status=entry.get('status',{})
            if status.get('status_str') == 'error': return ('failed',json.dumps(status,ensure_ascii=False))
            if status.get('status_str') == 'success' or status.get('completed') is True:
                if status.get('status_str') not in (None,'success'): return ('failed',json.dumps(status,ensure_ascii=False))
                videos=[]
                for node in entry.get('outputs',{}).values():
                    for kind in ('videos','images','gifs'):
                        for item in node.get(kind,[]):
                            if item.get('filename','').lower().endswith(('.mp4','.webm','.mov')):
                                videos.append(item)
                if not videos: return ('failed','Workflow completed without a video in history outputs')
                if len(videos)!=1: return ('failed','Expected one video; inspect ComfyUI history')
                item=videos[0]
                params=parse.urlencode({'filename':item['filename'],'subfolder':item.get('subfolder',''),'type':item.get('type','output')})
                return ('succeeded',self.base+'/view?'+params)
            return ('running',None)
        queue=self._request('/queue')
        self.last_inspection['queue']={field:[item for item in queue.get(field,[]) if (isinstance(item,list) and len(item)>1 and item[1]==prompt_id) or (isinstance(item,dict) and item.get('prompt_id')==prompt_id)] for field in ('queue_running','queue_pending')}
        for field in ('queue_running','queue_pending'):
            for item in queue.get(field,[]):
                if (isinstance(item,list) and len(item)>1 and item[1]==prompt_id) or (isinstance(item,dict) and item.get('prompt_id')==prompt_id):
                    return ('running',None)
        return ('unknown',None)

def h3_workflow(prompt, seed=42, width=864, height=480, frames=124, steps=20, prefix='videoflow/output'):
    if width<=0 or height<=0 or width%32 or height%32: raise ValueError('width and height must be positive multiples of 32')
    if not 124<=frames<=362 or (frames-5)%17: raise ValueError('frames must be 124..362 in steps of 17')
    if steps<1 or not 0<=seed<2**64: raise ValueError('invalid steps or seed')
    return {
      '1':{'class_type':'UNETLoader','inputs':{'unet_name':'minimax_h3_fl2va_pruned_int8_convrot.safetensors','weight_dtype':'default'}},
      '2':{'class_type':'CLIPLoader','inputs':{'clip_name':'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors','type':'minimax','device':'default'}},
      '3':{'class_type':'VAELoader','inputs':{'vae_name':'minimax_h3_video_vae_int8_convrot.safetensors'}},
      '4':{'class_type':'VAELoader','inputs':{'vae_name':'minimax_h3_audio_vae_fp32.safetensors'}},
      '5':{'class_type':'MiniMaxH3ImageToVideo','inputs':{'prompt':prompt,'width':width,'height':height,'length':frames,'clip':['2',0],'vae':['3',0]}},
      '6':{'class_type':'BasicGuider','inputs':{'model':['1',0],'conditioning':['5',0]}},
      '7':{'class_type':'BasicScheduler','inputs':{'model':['1',0],'scheduler':'simple','steps':steps,'denoise':1.0}},
      '8':{'class_type':'KSamplerSelect','inputs':{'sampler_name':'res_multistep'}},
      '9':{'class_type':'RandomNoise','inputs':{'noise_seed':seed}},
      '10':{'class_type':'SamplerCustomAdvanced','inputs':{'noise':['9',0],'guider':['6',0],'sampler':['8',0],'sigmas':['7',0],'latent_image':['5',1]}},
      '11':{'class_type':'VAEDecode','inputs':{'samples':['10',0],'vae':['3',0]}},
      '12':{'class_type':'VAEDecodeAudio','inputs':{'samples':['10',0],'vae':['4',0]}},
      '13':{'class_type':'CreateVideo','inputs':{'fps':24,'bit_depth':8,'images':['11',0],'audio':['12',0]}},
      '14':{'class_type':'SaveVideo','inputs':{'filename_prefix':prefix,'format':'auto','codec':'auto','video':['13',0]}}}

def render_template(template, prompt, node, field):
    graph=copy.deepcopy(template)
    if not isinstance(graph,dict) or not isinstance(graph.get(node),dict) or field not in graph[node].get('inputs',{}):
        raise ValueError('prompt node/input not found in API-format workflow')
    graph[node]['inputs'][field]=prompt
    return graph

