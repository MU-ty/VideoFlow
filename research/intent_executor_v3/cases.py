"""Author-written post-V2 diagnostic; two surface variants per eval family.
Gold literal updates are authored separately from operation programs, never executed
by the tested engine. Not independently authored or semantic-family held out.
"""
from copy import deepcopy


def setop(s,f,v): return dict(op='set',shot=s,field=f,value=v)
def restore(s,f,t): return dict(op='restore_before',shot=s,field=f,turn=t)
def copyop(s,f,src): return dict(op='copy_current',shot=s,field=f,source=src)


def suite():
    cases=[]
    for split, variants, families in [('dev', [0], ['control','scoped','nested','copy']),
                                      ('eval',[1,2],['control','scoped','nested','copy','mixed','noop'])]:
        for v in variants:
            a,b,c=[f'S{i+v*6}' for i in (2,4,6)]
            # Vary names and text, not claim new independent semantic families.
            word=['序幕','候船','归程'][v]; other=['尾声','靠港','散场'][v]
            initial={s:dict(color=col,light='day',subtitle=word) for s,col in [(a,'white'),(b,'red'),(c,'black')]}
            for family in families:
                steps=[]; state=deepcopy(initial)
                def add(text,ops,updates):
                    for s,f,value in updates: state[s][f]=value
                    steps.append(dict(instruction=text,annotation_ops=ops,
                                      expected=deepcopy(state),addressed=sorted({(o['shot'],o['field']) for o in ops})))
                def literal(text,updates): add(text,[setop(*u) for u in updates],updates)
                if family=='control':
                    literal(f'{a}颜色设blue，{b}字幕设“{other}”。',[(a,'color','blue'),(b,'subtitle',other)])
                    literal('所有镜头照明设sunset，其余不动。',[(s,'light','sunset') for s in (a,b,c)])
                    literal(f'仅{c}颜色设white。',[(c,'color','white')])
                    literal(f'{a}字幕设“完”，{b}颜色设black。',[(a,'subtitle','完'),(b,'color','black')])
                elif family=='scoped':
                    literal('所有镜头颜色设blue。',[(s,'color','blue') for s in (a,b,c)])
                    literal(f'{a}颜色设red，{b}颜色设white。',[(a,'color','red'),(b,'color','white')])
                    literal(f'{a}照明设sunset，{c}字幕设“{other}”。',[(a,'light','sunset'),(c,'subtitle',other)])
                    add(f'仅恢复{a}颜色到第2轮开始前；其他字段保留当前值。',[restore(a,'color',2)],[(a,'color','blue')])
                    literal(f'{b}字幕设“完成”。',[(b,'subtitle','完成')])
                elif family=='nested':
                    literal(f'{b}颜色设blue。',[(b,'color','blue')])
                    literal(f'{b}颜色设black。',[(b,'color','black')])
                    add(f'撤销第2轮中{b}颜色的修改。',[restore(b,'color',2)],[(b,'color','blue')])
                    literal(f'{c}字幕设“{other}”，{b}照明设sunset。',[(c,'subtitle',other),(b,'light','sunset')])
                    add(f'撤销第3轮对{b}颜色的恢复操作，也就是取第3轮开始前该字段的值。',[restore(b,'color',3)],[(b,'color','black')])
                elif family=='copy':
                    add(f'把本轮开始时{a}颜色复制给{b}。',[copyop(b,'color',a)],[(b,'color','white')])
                    literal(f'{a}颜色设blue，{b}字幕设“{other}”；不联动复制。',[(a,'color','blue'),(b,'subtitle',other)])
                    add(f'把本轮开始时{b}颜色复制给{c}，同时{b}颜色设black。',[copyop(c,'color',b),setop(b,'color','black')],[(c,'color','white'),(b,'color','black')])
                    add(f'只把{b}颜色恢复到第3轮开始前。',[restore(b,'color',3)],[(b,'color','white')])
                    add(f'仅撤销第1轮的{b}颜色复制操作，其余保留。',[restore(b,'color',1)],[(b,'color','red')])
                elif family=='mixed':
                    literal(f'{c}字幕设“{other}”，颜色设blue。',[(c,'subtitle',other),(c,'color','blue')])
                    literal(f'{c}字幕设“暂定”，颜色设white，照明设sunset。',[(c,'subtitle','暂定'),(c,'color','white'),(c,'light','sunset')])
                    literal(f'{c}字幕设“定稿”，颜色设red；{a}颜色设black。',[(c,'subtitle','定稿'),(c,'color','red'),(a,'color','black')])
                    add(f'仅{c}字幕恢复到第2轮开始前，{c}颜色恢复到第3轮开始前；其余保留当前值。',[restore(c,'subtitle',2),restore(c,'color',3)],[(c,'subtitle',other),(c,'color','white')])
                    literal(f'{c}颜色现在设black，保留刚才的字幕。',[(c,'color','black')])
                else:
                    literal(f'{a}字幕设“{other}”。',[(a,'subtitle',other)])
                    literal(f'{a}字幕仍设“{other}”，同时{b}照明设sunset。',[(a,'subtitle',other),(b,'light','sunset')])
                    literal(f'{a}字幕设“新标题”，{b}颜色设blue。',[(a,'subtitle','新标题'),(b,'color','blue')])
                    add(f'仅让{a}字幕取第2轮开始前的文本；{b}当前各字段不变。',[restore(a,'subtitle',2)],[(a,'subtitle',other)])
                    add('本轮不修改任何字段。',[],[])
                cases.append(dict(id=f'{split}-{family}-{v}',split=split,family=family,initial=deepcopy(initial),turns=steps))
    return cases
