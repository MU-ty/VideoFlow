# 生成中的动态编辑实验 v0.4

新增 `run_dynamic.py`，在客户端还未观察到视频任务终态时应用修改。默认事件序列为：

1. 至少记录 2 次视频提交意图，并且 S2.video 第 1 版仍处于未决状态时，将主角服装改为黑色。
2. S2.video 第 2 版仍处于未决状态时，再改为蓝色。
3. 等待当前第 3 个项目版本可用，归档最终资产，并继续查询所有已失效的旧任务，直到它们都返回终态。

“未决”包含 submitting、submitted、unknown；要求该任务已有持久化提交意图。它是客户端状态，不保证 GPU 此刻物理上仍在运行，也可能是服务端已完成但客户端尚未查询。当前不是固定墙钟时刻的事件到达实验。

## 运行

无 GPU 的机制演示：

```bash
python research/mock_dynamic.py --output runs/dynamic-mock
python research/compare_dynamic.py runs/dynamic-mock/full runs/dynamic-mock/dair --output runs/dynamic-mock/comparison.json
```

演示后端返回的是提示词字节，不是可播放视频。提交的 `results/dynamic_mock.json` 是这个合成实验的结果。

| 方法 | 全序列视频提交意图 | 观察到的旧版本成功结果 | 观测到的未决任务峰值 |
| --- | ---: | ---: | ---: |
| Full | 10 | 4 | 2 |
| DAIR | 8 | 2 | 2 |

两组实验均使用 2 个未决任务槽位、相同输入和相同客户端状态触发规则。这里的数字不能解释为真实生成成本下降；也没有视频质量、一致性或编辑成功率结论。每种方法会在自己的执行进度上触发事件，因此不等于使用相同的绝对时间事件轨迹。

在已有 ComfyUI/H3 环境运行：

```bash
python research/run_dynamic.py --method full --server http://127.0.0.1:8188 --output runs/dynamic-full --max-inflight 2 --probe
python research/run_dynamic.py --method dair --server http://127.0.0.1:8188 --output runs/dynamic-dair --max-inflight 2 --probe
python research/compare_dynamic.py runs/dynamic-full runs/dynamic-dair --output runs/dynamic-comparison.json
```

`--probe` 需要 ffprobe，只验证媒体容器/视频流，不验证语义。`--manifest` 和 `--schedule` 可以指定自己的资产和事件。保持相同 output 目录及配置再次运行可恢复；`--max-passes 0` 可初始化而不执行 worker。正常完成退出码为 0，暂停、阻塞和错误为 2。

## 事件文件

默认事件位于 `fixtures/dynamic_edits.json`。trigger 支持：

| 字段 | 含义 |
| --- | --- |
| after_submission_intents | 所有视频任务累计的持久化提交意图数至少达到此值，默认 0 |
| asset 与 revision | 同时提供时，要求这个视频资产的指定版本仍为客户端未决任务 |

事件按列表顺序依次触发。后一个事件不能越过前一个事件。expected_revision 可省略，执行时按项目版本依次设置为 1、2……；显式提供时必须符合这个顺序。每个事件必须有唯一的非空字符串 id。

触发器的 asset revision 是资产版本，expected_revision 是项目版本，两者不是同一个计数器。默认例子连续编辑同一个目标，所以目标资产版本与项目版本同步；其他事件组合需要分别核查。

修改事务仍使用 DAIR/Full 的版本检查和事件幂等记录。恢复游标以数据库 `vf_events` 的连续前缀为准，不依赖 JSON 状态文件中的下标。即使“修改已经提交、事件日志还没写入”时中断，也不会再次执行该修改。检测到配置不符或计划外的数据库编辑会拒绝继续。

如果项目已完成，但下一条触发条件从未达到，报告 blocked，不能假装整个事件序列执行成功。触发器必须适合两种方法；不应把无法触发的事件删掉后只统计成功样本。

## 旧版本任务与并发槽位

逻辑任务失效不等于远端工作取消。运行时新增独立的 remote_state、remote_observed_at、remote_terminal_at 和 remote_output。

- superseded 任务继续按原 backend、prompt_id 查询。
- 远端仍未决的旧任务继续占用 runner 的槽位，避免连续编辑使实际提交失去约束。
- 旧任务成功时记录来源和结果地址，但 complete 返回 stale，不写入当前资产。
- 旧任务失败只结束它自己的远端状态，不将当前新版本改成失败。
- 延迟到达的非终态观测不会覆盖已记录的终态；相互冲突的终态结果会拒绝。

槽位只管理这个 runner 跟踪的任务，不包含其他进程或人工在 ComfyUI 提交的工作。未知任务不会自动重提；如果服务端永久清理了历史记录，它可能长期占槽，需要人工对账。

最终项目已归档但仍有旧任务未知时，状态为 paused、phase 为 final_ready_waiting_remote，project_ready 为 true。只有当前产物可用且所有旧任务均返回终态，实验才报告 complete。旧视频文件不额外下载，remote_output 仅记录服务端结果地址；无法据此推算实际 GPU 成本。

## 结果与延迟

`report.json` 记录每次事件的应用时间、计划、当前版本及远端未决任务。`events.jsonl` 是辅助观测日志；真实事件记录以 SQLite 为准。最终归档仅包含当前版本，旧结果另列为 stale_observations。

中间项目版本可能从未完成。例如黑衣版本生成时再次修改，黑衣项目版本就不会被发布。对应 own_revision_latency_seconds 保持 null；event_to_final_revision_observed_seconds 表示从该事件到最终版本可用，包含后续修改的耗时，不能当成单次编辑延迟。

计划中的 reuse 包含保持不变的 pending/running 工作，不一定是已经生成的素材。这里没有把计划复用数报成安全成品复用率。

当前 67 项测试通过，覆盖连续编辑、编辑事务后中断、未知旧任务占槽、旧失败不污染新版本、终态观测不回退，以及实际 ComfyUI HTTP 适配器对旧任务的查询。所有模型行为仍来自 mock 后端或本地 HTTP 服务；没有接入实际 GPU。本阶段尚未加入固定墙钟事件轨迹、视觉质量评估、自动依赖提取或真正的参考图加载。
