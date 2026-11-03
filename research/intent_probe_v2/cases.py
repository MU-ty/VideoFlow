"""V2 post-V1 exploratory diagnostics. Author annotations, not user logs."""
import copy


def suite():
    output=[]
    def add(name,split,family,turns,initial=None):
        state=copy.deepcopy(initial or {s:dict(color=c,light='day',subtitle='开场') for s,c in [('S1','black'),('S3','white'),('S5','red')]})
        origin=copy.deepcopy(state);snapshots=[];steps=[]
        for instruction,ops in turns:
            snapshots.append(copy.deepcopy(state));addressed=set()
            for op in ops:
                if op[0]=='restore':
                    _,turn,shot,key=op
                    for target in state if shot=='all' else [shot]:
                        state[target][key]=snapshots[turn-1][target][key];addressed.add((target,key))
                elif op[0]=='copy':
                    _,src,dst,key=op;state[dst][key]=snapshots[-1][src][key];addressed.add((dst,key))
                else:
                    shot,key,value=op
                    for target in state if shot=='all' else [shot]:
                        state[target][key]=value;addressed.add((target,key))
            steps.append(dict(instruction=instruction,expected=copy.deepcopy(state),addressed=sorted(addressed),annotation_ops=ops))
        output.append(dict(id=name,split=split,family=family,initial=origin,turns=steps))
    add('dev-control','dev','control',[
        ('S1颜色改蓝，S3字幕写“启程”。',[('S1','color','blue'),('S3','subtitle','启程')]),
        ('所有镜头照明用sunset；其余不动。',[('all','light','sunset')]),
        ('S5颜色改白，S1字幕写“结束”。',[('S5','color','white'),('S1','subtitle','结束')])])
    add('dev-color','dev','scoped_color',[
        ('S1颜色从当前值换为红色。',[('S1','color','red')]),
        ('S1换白，S3换黑。',[('S1','color','white'),('S3','color','black')]),
        ('S5字幕写“等候”。',[('S5','subtitle','等候')]),
        ('仅回退第2轮对S1颜色的操作，S3与字幕保持现状。',[('restore',2,'S1','color')])])
    add('dev-light','dev','scoped_light',[
        ('所有镜头照明改sunset。',[('all','light','sunset')]),
        ('S3和S5照明改day。',[('S3','light','day'),('S5','light','day')]),
        ('S3字幕写“日落”。',[('S3','subtitle','日落')]),
        ('让S3的照明回到第2轮执行之前的值，其余字段均保留当前值。',[('restore',2,'S3','light')])])
    add('dev-subtitle','dev','scoped_subtitle',[
        ('S5字幕先用“欢迎”。',[('S5','subtitle','欢迎')]),
        ('S5字幕再用“再见”，颜色改蓝。',[('S5','subtitle','再见'),('S5','color','blue')]),
        ('只撤回第2轮的S5字幕改动；颜色不撤回。',[('restore',2,'S5','subtitle')])])
    add('eval-control-fields','eval','control',[
        ('S3颜色换红色，S5字幕写“第一幕”。',[('S3','color','red'),('S5','subtitle','第一幕')]),
        ('S1照明换sunset，S3字幕写“第二幕”。',[('S1','light','sunset'),('S3','subtitle','第二幕')]),
        ('S5颜色换黑色，其他维持。',[('S5','color','black')])])
    add('eval-control-copy','eval','control',[
        ('本轮把S1颜色复制给S3。',[('copy','S1','S3','color')]),
        ('S1改白色，不联动此前复制结果。',[('S1','color','white')]),
        ('把本轮开始时S3颜色复制给S5；S1照明改sunset。',[('copy','S3','S5','color'),('S1','light','sunset')])])
    add('eval-subtitle-history','eval','scoped_subtitle',[
        ('S1和S5字幕均写“起航”。',[('S1','subtitle','起航'),('S5','subtitle','起航')]),
        ('S1字幕换“靠岸”，S5字幕换“等候”。',[('S1','subtitle','靠岸'),('S5','subtitle','等候')]),
        ('S1照明改sunset。',[('S1','light','sunset')]),
        ('只让S5字幕取第2轮开始前的文本；S1字幕、光照不变。',[('restore',2,'S5','subtitle')]),
        ('S3颜色改蓝，刚才恢复的字幕保持。',[('S3','color','blue')])])
    add('eval-light-exception','eval','scoped_light',[
        ('三镜头都设sunset。',[('all','light','sunset')]),
        ('只把S1与S5的照明调回day。',[('S1','light','day'),('S5','light','day')]),
        ('S1颜色用白色，S5字幕用“晨间”。',[('S1','color','white'),('S5','subtitle','晨间')]),
        ('撤销第2轮涉及S5照明的那部分操作；S1仍是day，字幕仍是“晨间”。',[('restore',2,'S5','light')])])
    add('eval-color-initial','eval','scoped_color',[
        ('S3与S5都换蓝色。',[('S3','color','blue'),('S5','color','blue')]),
        ('S3换红色，S5换白色。',[('S3','color','red'),('S5','color','white')]),
        ('S3照明换sunset。',[('S3','light','sunset')]),
        ('把S3颜色恢复到第2轮之前，S5继续白色，照明保持当前。',[('restore',2,'S3','color')])])
    add('eval-undo-undo','eval','undo_undo',[
        ('S1颜色换蓝色。',[('S1','color','blue')]),
        ('S1颜色换红色。',[('S1','color','red')]),
        ('回退第2轮S1颜色修改。',[('restore',2,'S1','color')]),
        ('S3字幕写“旁白”。',[('S3','subtitle','旁白')]),
        ('现在回退第3轮对S1颜色的操作；旁白不撤销。',[('restore',3,'S1','color')])])
    add('eval-noop','eval','noop_history',[
        ('S5颜色改蓝。',[('S5','color','blue')]),
        ('S5颜色再设为蓝色，同时照明设sunset。',[('S5','color','blue'),('S5','light','sunset')]),
        ('S5颜色换黑。',[('S5','color','black')]),
        ('仅将S5颜色恢复到第2轮开始前；照明维持sunset。',[('restore',2,'S5','color')])])
    add('eval-copy-revert','eval','copy_undo',[
        ('S1颜色复制给S5。',[('copy','S1','S5','color')]),
        ('S1颜色换白色。',[('S1','color','white')]),
        ('S5颜色换红色，字幕写“重拍”。',[('S5','color','red'),('S5','subtitle','重拍')]),
        ('只还原第3轮对S5颜色的改动，字幕保留。',[('restore',3,'S5','color')]),
        ('S3颜色取本轮开始时S5的颜色。',[('copy','S5','S3','color')])])
    add('eval-two-rounds','eval','multi_round',[
        ('S3颜色改蓝。',[('S3','color','blue')]),
        ('S3字幕改“湖边”，照明改sunset。',[('S3','subtitle','湖边'),('S3','light','sunset')]),
        ('S3颜色改黑，字幕改“山顶”。',[('S3','color','black'),('S3','subtitle','山顶')]),
        ('颜色回到第3轮开始前，字幕回到第2轮开始前；照明保持。',[('restore',3,'S3','color'),('restore',2,'S3','subtitle')])])
    add('eval-after-rollback','eval','later_override',[
        ('S5颜色改白。',[('S5','color','white')]),
        ('S5颜色改黑。',[('S5','color','black')]),
        ('撤销第2轮的S5颜色修改。',[('restore',2,'S5','color')]),
        ('S5颜色改蓝；不要延续先前的白色要求。',[('S5','color','blue')]),
        ('S5字幕改“完成”。',[('S5','subtitle','完成')])])
    add('eval-multifield','eval','partial_fields',[
        ('S1颜色改白、照明改sunset、字幕写“夜行”。',[('S1','color','white'),('S1','light','sunset'),('S1','subtitle','夜行')]),
        ('S1颜色改红、照明改day、字幕写“晨行”。',[('S1','color','red'),('S1','light','day'),('S1','subtitle','晨行')]),
        ('S3颜色改蓝。',[('S3','color','blue')]),
        ('回退第2轮中S1的颜色与字幕两项；S1照明保留day，S3继续蓝色。',[('restore',2,'S1','color'),('restore',2,'S1','subtitle')])])
    add('eval-global-slice','eval','global_slice',[
        ('所有镜头颜色改白。',[('all','color','white')]),
        ('S1改红、S3改蓝、S5改黑。',[('S1','color','red'),('S3','color','blue'),('S5','color','black')]),
        ('所有镜头字幕写“保留”。',[('all','subtitle','保留')]),
        ('仅将S1与S3的颜色恢复到第2轮之前，S5仍黑色；字幕不改。',[('restore',2,'S1','color'),('restore',2,'S3','color')])])
    return output
