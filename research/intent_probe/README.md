# 连续修改意图诊断 v1

问题：同一模型是否会误解局部例外、部分撤销、历史值复制；显式历史约束记录是否优于完整历史加当前状态表？

这是**待运行的模型实验**。五项本地测试只验证注释、提示隔离、评分和失败记录，不是模型成功率。无视频生成，不连接 ComfyUI，不测视频质量。

## 数据与证据边界

30条序列114轮，三个外套/浇水壶/背包语境各10条；其中6条简单控制序列。颜色、光照、字幕三字段用于检查用户要求与非目标保持。10条外套序列用于开发；其余20条作为后续探索性评估。三语境共享十种模板，因此**不是30个独立故事，也不是严格独立的留出任务**。自然语言和操作注释都由同一作者编写，仍需人工核对。没有模糊意图题，也不能推断真实用户出错频率。

标准状态由明确标注的set、restore和copy语义执行生成；它只解释标注，不解析自然语言。恢复第n轮指定字段=恢复该轮开始前的值；复制是当轮快照而非持续绑定。此约定对所有方法公开。金标准、操作标注、题型、split均不发给模型。

三方法使用同一模型、temperature=0、输出上限和完整历史（含模型自己的历史输出）：

- history：从完整历史重建状态。
- state：历史+最近预测状态表，并提示检查作用域、例外、撤销和复制。这是主要强提示基线。
- ledger：同样输入，加模型自己产生的结构化before/after/覆盖记录；不注入标准记录。

当前ledger是**表示与提示消融**，不是已实现的自动约束推理算法。它的额外输出和上下文成本必须计入。每轮均输出完整状态，不隐藏中间错误；记录由模型自行生成，错误可能累积。格式错误本轮计错，下一轮保留最近有效预测，并提供原始错误输出；请求错误停止，绝不自动重发。记录pending不能证明供应商是否已计费。

## 运行

Python 3.10+，标准库，无pip依赖。在仓库根目录：

```bash
python -m unittest discover -s research/intent_probe -p 'test_*.py' -v
python research/intent_probe/run.py prepare --output runs/intent-probe-v1
```

准备只生成冻结协议与SHA256，不调用模型。使用可访问的 **Chat Completions兼容**服务；ComfyUI的8188并非语言模型服务。请在本地环境配置，不要提交密钥：

```bash
export INTENT_BASE_URL='http://127.0.0.1:8000/v1'
export INTENT_MODEL='实际部署的模型名'
read -rsp 'API key: ' INTENT_API_KEY
export INTENT_API_KEY
python -u research/intent_probe/run.py run --output runs/intent-probe-v1 --split dev
python research/intent_probe/run.py score --output runs/intent-probe-v1 --split dev
```

若本地服务无认证，可输入本地服务接受的非空占位值。远程服务使用HTTPS。先运行开发集，核对服务是否接受`messages/temperature/max_tokens`，API兼容性依供应商而定。脚本不自动下载或启动模型。

开发集每次重复为38轮×3方法=114次请求；评估集76轮×3=228次。默认重复1次；`--repeats 3`变为三倍。重复温度0调用不等于三个随机种子。调用前确认服务成本及上下文容量；超长或截断同样记录为失败，不悄悄重试。

开发阶段结束、冻结配置后，在相同协议上首次评估：

```bash
python -u research/intent_probe/run.py run --output runs/intent-probe-v1 --split eval
python research/intent_probe/run.py score --output runs/intent-probe-v1 --split eval
```

每条序列每种方法有独立记录，运行配置固定。已有结果或pending会阻止覆盖；本版不支持自动续跑。失败后先score检查已完成部分；需要重跑请创建新输出目录，记录重跑理由，不择优替换。不能在查看eval后调整方法再把同一eval称为新验证。

## 指标与决策

`*-run/report.txt`包含全序列正确率、逐轮正确数、撤销轮正确数、未要求字段变化、格式/请求失败、分题型结果、延迟、服务返回token统计及ledger/state逐序列配对胜平负。缺少序列在全序列分母中算失败；缺少usage时tokens只是已观察下界。未要求字段错误可能来自先前累积错误，不能直接解释为当前轮新增破坏。

先回答baseline是否真的出错，再看ledger能否纠正。如果state已接近全对，或ledger不能稳定超过state，暂停路线。即便出现正差，也仅构成探索信号；需核对控制题无退步、额外token/延迟，以及收益是否集中于单模板。每个故事模板作为簇报告，不把114轮当独立样本。不做显著性或泛化结论。跨模型、独立作者的新自然语言序列和最终真实视频执行是后续验证，不能用本轮替代。

协议哈希锁住数据、提示与规则；修改请建立新版本与新结果目录。当前不写论文性能结论。
