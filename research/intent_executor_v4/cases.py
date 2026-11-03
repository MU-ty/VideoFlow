"""Fresh author-written trajectories. Gold literals independent of engine.
Authored after V3; not independently authored or new semantic families.
"""
from copy import deepcopy


def S(s,f,v): return dict(op='set',shot=s,field=f,value=v)
def R(s,f,t): return dict(op='restore_before',shot=s,field=f,turn=t)
def C(s,f,src): return dict(op='copy_current',shot=s,field=f,source=src)


class Case:
    def __init__(self,name,split,family,colors=('red','white','blue')):
        self.initial={s:dict(color=c,light='day',subtitle='片头') for s,c in zip(('S21','S22','S23'),colors)}
        self.state=deepcopy(self.initial); self.steps=[]
        self.name,self.split,self.family=name,split,family
    def add(self,instruction,ops,literals):
        for s,f,v in literals:self.state[s][f]=v
        self.steps.append(dict(instruction=instruction,annotation_ops=ops,expected=deepcopy(self.state),
                               addressed=sorted({(o['shot'],o['field']) for o in ops})))
        return self
    def set(self,instruction,*updates):return self.add(instruction,[S(*u) for u in updates],updates)
    def done(self):return dict(id=self.name,split=self.split,family=self.family,initial=self.initial,turns=self.steps)


def suite():
    out=[]
    c=Case('dev-basics','dev','control')
    c.set('S22字幕写“入场”，S23颜色设black。',('S22','subtitle','入场'),('S23','color','black'))
    c.set('仅S21照明设sunset。',('S21','light','sunset'))
    c.add('本轮所有字段保持不变。',[],[]);out.append(c.done())
    c=Case('dev-restore','dev','scoped')
    c.set('S22照明设sunset。',('S22','light','sunset'))
    c.set('S22照明设day，字幕写“庭院”。',('S22','light','day'),('S22','subtitle','庭院'))
    c.add('仅S22照明恢复到第2轮开始前，字幕保持。',[R('S22','light',2)],[('S22','light','sunset')]);out.append(c.done())
    c=Case('dev-snapshot','dev','copy')
    c.add('本轮开始时S21颜色复制给S23；同时S21颜色设white。',[C('S23','color','S21'),S('S21','color','white')],[('S23','color','red'),('S21','color','white')])
    c.set('S23颜色设black。',('S23','color','black'))
    c.add('仅恢复S23颜色到第2轮开始前。',[R('S23','color',2)],[('S23','color','red')]);out.append(c.done())
    c=Case('dev-two-fields','dev','mixed')
    c.set('S21字幕写“开始”，照明设sunset。',('S21','subtitle','开始'),('S21','light','sunset'))
    c.set('S21字幕写“结束”，照明设day。',('S21','subtitle','结束'),('S21','light','day'))
    c.add('仅S21字幕恢复到第2轮前，照明恢复到第1轮前。',[R('S21','subtitle',2),R('S21','light',1)],[('S21','subtitle','开始'),('S21','light','day')]);out.append(c.done())

    c=Case('eval-station','eval','control')
    c.set('S21字幕写“车站”，S22照明设sunset。',('S21','subtitle','车站'),('S22','light','sunset'))
    c.set('三个镜头颜色都设white。',*((s,'color','white') for s in c.state))
    c.set('只把S23字幕改成“末班车”。',('S23','subtitle','末班车'))
    c.set('S21颜色设black，S23照明设sunset。',('S21','color','black'),('S23','light','sunset'))
    c.add('不做任何修改，保持当前项目。',[],[]);out.append(c.done())
    c=Case('eval-stage','eval','control',('black','red','white'))
    c.set('所有镜头字幕写“演出”。',*((s,'subtitle','演出') for s in c.state))
    c.set('S22颜色设blue，S21照明设sunset。',('S22','color','blue'),('S21','light','sunset'))
    c.add('把本轮开始时S22颜色复制给S23。',[C('S23','color','S22')],[('S23','color','blue')])
    c.set('S22改为white，不联动之前的复制。',('S22','color','white'))
    c.set('S21字幕写“谢幕”，其余保持。',('S21','subtitle','谢幕'));out.append(c.done())

    c=Case('eval-gallery','eval','scoped')
    c.set('S21和S23照明设sunset。',('S21','light','sunset'),('S23','light','sunset'))
    c.set('S21字幕写“展厅”，S22颜色设black。',('S21','subtitle','展厅'),('S22','color','black'))
    c.set('S21与S23照明都设day。',('S21','light','day'),('S23','light','day'))
    c.set('S23字幕写“闭馆”。',('S23','subtitle','闭馆'))
    c.add('只撤销第3轮对S21照明的操作。S23照明和所有字幕不变。',[R('S21','light',3)],[('S21','light','sunset')])
    c.set('S22字幕写“保留”。',('S22','subtitle','保留'));out.append(c.done())
    c=Case('eval-cafe','eval','scoped',('white','blue','black'))
    c.set('三个镜头字幕都写“上午”。',*((s,'subtitle','上午') for s in c.state))
    c.set('S21字幕改“午后”，S22字幕改“打烊”。',('S21','subtitle','午后'),('S22','subtitle','打烊'))
    c.set('S22颜色设red，S23照明设sunset。',('S22','color','red'),('S23','light','sunset'))
    c.set('S21字幕再改“夜晚”。',('S21','subtitle','夜晚'))
    c.add('仅S22字幕取第2轮开始前的文本；S21字幕仍是当前的“夜晚”。',[R('S22','subtitle',2)],[('S22','subtitle','上午')]);out.append(c.done())

    c=Case('eval-bridge','eval','nested')
    c.set('S23颜色设white。',('S23','color','white'))
    c.set('S21字幕写“桥头”。',('S21','subtitle','桥头'))
    c.set('S23颜色设red，照明设sunset。',('S23','color','red'),('S23','light','sunset'))
    c.add('仅撤销第3轮的S23颜色修改。',[R('S23','color',3)],[('S23','color','white')])
    c.set('S22字幕写“远景”。',('S22','subtitle','远景'))
    c.add('撤销第4轮的S23颜色恢复操作，即该颜色回到第4轮开始前。',[R('S23','color',4)],[('S23','color','red')]);out.append(c.done())
    c=Case('eval-radio','eval','nested')
    c.set('S22字幕写“直播”，照明设sunset。',('S22','subtitle','直播'),('S22','light','sunset'))
    c.set('S22字幕改“录播”。',('S22','subtitle','录播'))
    c.add('撤销第2轮S22字幕修改。',[R('S22','subtitle',2)],[('S22','subtitle','直播')])
    c.add('撤销第3轮S22字幕恢复操作。',[R('S22','subtitle',3)],[('S22','subtitle','录播')])
    c.add('仅将S22字幕恢复到第4轮开始前。',[R('S22','subtitle',4)],[('S22','subtitle','直播')])
    c.set('S21颜色设blue。',('S21','color','blue'));out.append(c.done())

    c=Case('eval-library','eval','copy')
    c.set('S21字幕写“借阅”，S22字幕写“归还”。',('S21','subtitle','借阅'),('S22','subtitle','归还'))
    c.add('本轮将S21和S22的字幕互换，均读取本轮开始时的值。',[C('S21','subtitle','S22'),C('S22','subtitle','S21')],[('S21','subtitle','归还'),('S22','subtitle','借阅')])
    c.set('S21字幕改“闭馆”。',('S21','subtitle','闭馆'))
    c.add('只恢复S21字幕到第3轮开始前。',[R('S21','subtitle',3)],[('S21','subtitle','归还')])
    c.add('只撤销第2轮对S22字幕的复制操作。',[R('S22','subtitle',2)],[('S22','subtitle','归还')]);out.append(c.done())
    c=Case('eval-workshop','eval','copy',('blue','black','white'))
    c.add('把本轮开始时S21颜色复制给S22，同时S21颜色设red。',[C('S22','color','S21'),S('S21','color','red')],[('S22','color','blue'),('S21','color','red')])
    c.add('本轮开始时S22颜色复制给S23。',[C('S23','color','S22')],[('S23','color','blue')])
    c.set('S22颜色设white，S23颜色设black。',('S22','color','white'),('S23','color','black'))
    c.add('仅S23颜色恢复到第3轮开始前。',[R('S23','color',3)],[('S23','color','blue')])
    c.add('仅S22颜色恢复到第1轮开始前。',[R('S22','color',1)],[('S22','color','black')])
    c.set('S21字幕写“收工”，其余保持。',('S21','subtitle','收工'));out.append(c.done())

    c=Case('eval-rooftop','eval','mixed')
    c.set('S21颜色设blue，字幕写“出发”。',('S21','color','blue'),('S21','subtitle','出发'))
    c.set('S21颜色设white，照明设sunset。',('S21','color','white'),('S21','light','sunset'))
    c.set('S21字幕改“抵达”，颜色改black。',('S21','subtitle','抵达'),('S21','color','black'))
    c.set('S22字幕写“旁白”。',('S22','subtitle','旁白'))
    c.add('S21颜色恢复到第3轮前，S21字幕恢复到第1轮前；其他字段不动。',[R('S21','color',3),R('S21','subtitle',1)],[('S21','color','white'),('S21','subtitle','片头')])
    c.set('S21字幕现在写“新起点”。',('S21','subtitle','新起点'));out.append(c.done())
    c=Case('eval-cinema','eval','mixed',('black','white','red'))
    c.set('S22照明设sunset，S23字幕写“预告”。',('S22','light','sunset'),('S23','subtitle','预告'))
    c.set('S22照明设day，S23字幕写“正片”，S21颜色设blue。',('S22','light','day'),('S23','subtitle','正片'),('S21','color','blue'))
    c.set('S23字幕写“彩蛋”，S21颜色设white。',('S23','subtitle','彩蛋'),('S21','color','white'))
    c.add('S22照明取第2轮开始前；S23字幕取第3轮开始前；S21颜色保持white。',[R('S22','light',2),R('S23','subtitle',3)],[('S22','light','sunset'),('S23','subtitle','正片')])
    c.add('本轮全部保持不变。',[],[]);out.append(c.done())

    c=Case('eval-rain','eval','noop')
    c.set('S23照明设sunset。',('S23','light','sunset'))
    c.set('S23照明再设sunset，同时S21字幕写“雨停”。',('S23','light','sunset'),('S21','subtitle','雨停'))
    c.add('本轮不改任何字段。',[],[])
    c.set('S23照明改day。',('S23','light','day'))
    c.add('仅S23照明恢复到第2轮开始前；S21字幕不动。',[R('S23','light',2)],[('S23','light','sunset')])
    c.add('保持全部字段，不作修改。',[],[]);out.append(c.done())
    c=Case('eval-market','eval','noop',('white','black','red'))
    c.set('S22颜色设red，字幕写“开市”。',('S22','color','red'),('S22','subtitle','开市'))
    c.add('本轮不作修改。',[],[])
    c.set('S22颜色再设red，S23字幕写“摊位”。',('S22','color','red'),('S23','subtitle','摊位'))
    c.set('S22颜色设blue，字幕写“休市”。',('S22','color','blue'),('S22','subtitle','休市'))
    c.add('S22颜色恢复到第3轮开始前，S22字幕恢复到第4轮开始前。',[R('S22','color',3),R('S22','subtitle',4)],[('S22','color','red'),('S22','subtitle','开市')]);out.append(c.done())
    return out
