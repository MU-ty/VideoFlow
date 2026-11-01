# 可恢复实验与产物归档 v0.3

本轮新增 Full 可执行基线、任务观测记录、内容归档、可恢复实验入口和匹配实验对比。运行协议为：**先完成并归档初始项目 → 应用一次结构化修改 → 完成并归档修改后的项目**。这一协议用于受控的编辑对照，不代表已完成生成中多事件交互的真实视频评测。

## 无 GPU 演示

在仓库根目录运行（Python 3.10+，主流程无第三方 Python 依赖）：

```bash
python -m unittest discover -s tests -v
python research/mock_experiment.py --output runs/mock
python research/compare_reports.py runs/mock/full runs/mock/dair --output runs/mock/comparison.json
```

MockProvider 只创建带提示词的合成字节，不生成可播放视频。提交的 `results/paired_mock.json` 仅是执行机制演示：Full 修改阶段有 6 次视频提交意图，DAIR 为 3 次；计入各自初始生成，分别为 12 和 9 次。此结果不能写成真实 GPU 成本或生成质量提升。

## 在 H3 服务器进行真实对照

在已有可用的 ComfyUI/H3 环境和 VideoFlow 仓库中运行：

```bash
python research/run_experiment.py --method full --server http://127.0.0.1:8188 --output runs/h3-full --max-inflight 1 --probe
python research/run_experiment.py --method dair --server http://127.0.0.1:8188 --output runs/h3-dair --max-inflight 1 --probe
python research/compare_reports.py runs/h3-full runs/h3-dair --output runs/h3-comparison.json
```

默认输入为 `fixtures/assets.json` 和 `fixtures/edit_costume.json`。两个条件各自从初始版本生成，种子及模型配置相同；示例总计计划提交 21 个视频任务。模型是否确定性、服务端是否执行重复任务、相同文件名对应的权重是否变化，仍需真实环境核查。

`--probe` 需要 PATH 中有 ffprobe。它验证视频流并记录容器、时长、编解码等信息，不能评估人物一致性、动作连贯性或编辑语义正确性。测试中使用 ffmpeg 创建的单色视频只验证 ffprobe 路径，不是 H3 生成证据。

可修改 `--manifest`、`--edit`、`--poll-seconds`、`--timeout-seconds` 和 `--max-inflight`。默认轮询间隔 5 秒、每次运行最长 7200 秒。`--max-passes 0` 可只初始化配置和数据库，不提交视频任务。正常完成退出码为 0；暂停、阻塞或错误退出码为 2。

## 暂停与恢复

使用**原命令和同一 output 目录**再次运行即可恢复。原任务通过持久化 prompt_id 查询；不会因为暂停就创建新任务。超时只暂停本次 runner，不取消远端 GPU 队列。

一个目录同时只允许一个实验 runner，SQLite 锁在进程退出后自动释放。配置、输入、后端、方法或执行源码发生变化时拒绝继续使用原目录，防止把不同版本结果混在一起。轮询间隔和单次运行上限可以变化，每次会话参数会被记录。修改源码后应使用新目录；不要在旧任务尚未对账时把新目录当成恢复手段。

当前并发上限计入 submitting/submitted/unknown 的视频任务，宁可保守阻塞，也不会因 unknown 提交替代任务。它适用于本实验 runner；不要同时用独立 worker 操作实验数据库。明确失败或工作流校验失败会输出 blocked，任务不自动重试。

## 文件与度量含义

| 文件 | 内容 |
| --- | --- |
| configuration.json | 完整输入、方法、后端、配置与执行源码指纹 |
| project.db | 原任务 ID、状态、输入与实际工作流、各版本资产 |
| state.json | 实验阶段、执行轮次、恢复会话 |
| observations.jsonl | 每轮 worker 的客户端观测日志 |
| report.json | 完成状态、编辑计划、时间代理值与明确的未测字段 |
| artifacts/revision-1.json、revision-2.json | 初始与修改后资产的清单、输入指纹和内容哈希 |
| artifacts/下的内容文件 | 视频二进制与字幕 UTF-8 文本；未变资产复用校验通过的本地文件 |

| 指标 | 解释与边界 |
| --- | --- |
| submission_intents | HTTP 提交前持久化的意图数；崩溃可能发生在 POST 之前，不等于计费次数 |
| accepted_observed_at | 客户端首次观察到提交成功或服务端仍在运行的时间 |
| reserve_to_completion_observed_seconds | 预约到完成观测的墙钟时间，包含排队、轮询与停机，不是 GPU 运行时间 |
| edit_completion_observed_seconds | 编辑提交到完整 Timeline manifest 可用的客户端时间，尚未计入之后的下载与人工评估 |
| estimated_edit_cost | 输入提供的任意单位估计值，不是实测费用 |
| gpu_seconds、api_cost、token_usage | 尚未测量，保持 null |
| edit_success_rate、cross_scene_consistency | 尚无人评或质量模型结果，保持 null |
| external_duplicate_executions | 服务端实际重复执行未知，保持 null |

时间使用客户端墙钟；系统校时会影响结果。旧数据库迁移时历史时间字段保持 NULL，不反推或伪造时间。对比工具要求两组输入、方法之外的配置和源码一致，并复核归档文件哈希；单个案例不能提供统计显著性或泛化证据。

## 归档行为

仅下载配置后端同源 `/view` 地址，拒绝重定向；单个文件默认上限 2 GiB。临时下载中断、大小不符或当前项目 revision 改变时，不发布本轮归档清单。内容使用 SHA-256 命名与复核；已有内容损坏时重新下载。输入指纹标识生成来源，内容哈希标识实际字节，两者不能混用。

归档在查询到任务完成后执行，因此服务端文件仍需保留到下载结束。SHA-256 不能证明服务端返回了语义正确的视频，也无法识别第一次下载前已被覆盖的同名输出。Timeline 仍是资产清单，暂未拼接视频、烧录字幕或写入外部 Draft。

当前仍没有真实参考图加载适配器、视觉一致性评估或独立故事盲测。本轮没有接入实际 GPU，提交的结果全部明确标为模拟。下一步应使用此入口采集真实产物，再进行编辑要求与跨场景一致性的独立评价。
