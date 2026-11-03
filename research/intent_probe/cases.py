"""Authored diagnostic cases, not natural user logs or independent stories."""
import copy


def suite():
    cases = []
    for domain, entity in [('costume', '外套'), ('greenhouse', '浇水壶'), ('harbor', '背包')]:
        initial = {s: {'color': 'white', 'light': 'day', 'subtitle': '原字幕'} for s in ['S2', 'S4', 'S6']}
        templates = [
            ('control', [(f'把三个镜头的{entity}都改成蓝色。', [('all','color','blue')]), ('只把 S4 改成黑色，其他保持。', [('S4','color','black')]), ('S2 的字幕改为“出发”。', [('S2','subtitle','出发')])]),
            ('control', [('三个镜头的光照都改成 sunset。', [('all','light','sunset')]), (f'只把 S6 的{entity}改成红色。', [('S6','color','red')]), ('S4 的字幕改为“等待”。', [('S4','subtitle','等待')])]),
            ('exception', [(f'三个镜头的{entity}都改成蓝色。', [('all','color','blue')]), ('S4 例外，改为黑色。', [('S4','color','black')]), ('三个镜头都改成 sunset 光照，颜色保持。', [('all','light','sunset')]), ('S6 字幕改为“登场”，其他保持。', [('S6','subtitle','登场')])]),
            ('partial_undo', [(f'三个镜头的{entity}都改成蓝色。', [('all','color','blue')]), ('S4 的颜色改成黑色。', [('S4','color','black')]), ('S4 字幕改为“停下”。', [('S4','subtitle','停下')]), ('仅撤销第2轮对颜色的修改；保留后来的字幕修改。', [('restore','2','S4','color')])]),
            ('snapshot', [(f'把 S2 的{entity}改为蓝色。', [('S2','color','blue')]), ('把 S2 改成红色，S4 改成黑色。', [('S2','color','red'),('S4','color','black')]), ('S2、S4 光照改成 sunset。', [('S2','light','sunset'),('S4','light','sunset')]), ('只把 S2 的颜色恢复为第2轮开始前的颜色，光照保持。', [('restore','2','S2','color')])]),
            ('copy_snapshot', [(f'把 S2 的{entity}改为蓝色。', [('S2','color','blue')]), ('S4 的颜色复制本轮开始时 S2 的颜色。', [('copy','S2','S4','color')]), ('S2 改为红色；此前复制到 S4 的颜色保持。', [('S2','color','red')]), ('S6 的颜色复制本轮开始时 S4 的颜色。', [('copy','S4','S6','color')])]),
            ('scoped_undo', [(f'三个镜头的{entity}都改成蓝色。', [('all','color','blue')]), ('S4 改成黑色，S6 改成红色。', [('S4','color','black'),('S6','color','red')]), ('三个镜头的光照都改成 sunset。', [('all','light','sunset')]), ('仅撤销第2轮对 S6 颜色的修改；S4 保持黑色，光照不变。', [('restore','2','S6','color')])]),
            ('repeat_undo', [(f'把 S2 的{entity}改成蓝色。', [('S2','color','blue')]), ('S2 改成红色。', [('S2','color','red')]), ('撤销第2轮的颜色修改，恢复为该轮开始前的颜色。', [('restore','2','S2','color')]), ('再撤销第3轮的颜色修改，恢复为第3轮开始前的颜色。', [('restore','3','S2','color')])]),
            ('override', [(f'三个镜头的{entity}都改成蓝色。', [('all','color','blue')]), ('S4 例外，改黑色。', [('S4','color','black')]), ('现在取消颜色例外，三个镜头一律改红色。', [('all','color','red')]), ('S6 字幕改为“到达”，其他保持。', [('S6','subtitle','到达')])]),
            ('mixed_undo', [(f'三个镜头的{entity}都改蓝色，光照都改 sunset。', [('all','color','blue'),('all','light','sunset')]), ('S4 的颜色改为黑色，字幕改为“等待”。', [('S4','color','black'),('S4','subtitle','等待')]), ('S6 的字幕改为“出发”。', [('S6','subtitle','出发')]), ('只撤销第1轮的光照修改；保留全部颜色和字幕修改。', [('restore','1','all','light')])]),
        ]
        for n, (family, turns) in enumerate(templates, 1):
            state = copy.deepcopy(initial); before = []; steps = []
            for text, ops in turns:
                before.append(copy.deepcopy(state)); changed = set()
                for op in ops:
                    if op[0] == 'restore':
                        _, turn, target, key = op
                        for shot in state if target == 'all' else [target]:
                            state[shot][key] = before[int(turn)-1][shot][key]; changed.add((shot,key))
                    elif op[0] == 'copy':
                        _, source, target, key = op
                        state[target][key] = before[-1][source][key]; changed.add((target,key))
                    else:
                        target, key, value = op
                        for shot in state if target == 'all' else [target]:
                            state[shot][key] = value; changed.add((shot,key))
                steps.append({'instruction': text, 'expected': copy.deepcopy(state), 'addressed': sorted(changed), 'annotation_ops': ops})
            cases.append({'id': f'{domain}-{n:02}', 'split': 'dev' if domain == 'costume' else 'eval', 'family': family, 'initial': initial, 'turns': steps})
    return cases
