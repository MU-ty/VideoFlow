# DAIR asset execution v0.4

DAIR 的结构化编辑计划现在可通过 `asset_runtime.py` 与 `asset_worker.py` 执行。旧 `worker.py` 保持原来的保守 Scene DAG 行为；新入口使用独立的 `vf_*` 表，一库一个项目，不会自动迁移旧 Scene 数据。

## 无 GPU 复现

在仓库根目录、Python 3.10+ 环境运行：

```bash
python -m unittest discover -s tests -v
python research/fault_benchmark.py
```

当前 67 项测试通过。故障探针输出 `results/runtime_faults.json`，每次重跑 local_wall_ms 会变化。

| 本地合成执行场景 | 已验证结果 |
| --- | --- |
| 已完成项目仅改字幕 | 更新 S2.subtitle，新增视频提交 0 次 |
| 后续主角服装修改 | 仅提交 S2.video、S4.video、S6.video 共 3 次 |
| 任务提交后重启 | 查询原任务，恢复阶段新增提交 0 次 |
| 提交已接受但响应丢失 | 保留 6 个 unknown 任务，不重提；原任务可查询后恢复 |
| 旧版本晚到 | 拒绝旧结果，当前 Timeline 不含旧产物 |

这些是确定性 mock 后端 + 真实本地 SQLite 的执行检查，不是统计恢复成功率或真实视频成本结果。新增 HTTP 集成测试通过本地模拟 ComfyUI 服务验证实际适配器的请求、500 响应后恢复及 `/view` URL 提取。

## 连接已有 ComfyUI

首先确认原 README 中的 H3 工作流能在你的 ComfyUI 独立执行。示例项目包含 6 个视频资产和 1 个字幕文本资产。

```bash
python asset_worker.py --db project-v2.db init fixtures/assets.json
python asset_worker.py --db project-v2.db run-once --server http://127.0.0.1:8188 --max-submit 1
python asset_worker.py --db project-v2.db status
```

反复运行同一条 `run-once` 来查询与提交。`--max-submit` 仅限制每轮新增视频提交数，不是 GPU 并发或排队上限。`--max-submit 0` 只恢复已有任务和执行已就绪的本地字幕任务。未知任务不会自动重试，失败任务也不会自动重提。

项目完成后，两种编辑任选其一：

```bash
python asset_worker.py --db project-v2.db edit fixtures/edit_costume.json
python asset_worker.py --db project-v2.db run-once --server http://127.0.0.1:8188 --max-submit 1
```

或者仅更新字幕：

```bash
python asset_worker.py --db project-v2.db edit fixtures/edit_subtitle.json
python asset_worker.py --db project-v2.db run-once --max-submit 0
```

两个示例事件的 `expected_revision` 均为 1。依次尝试两个事件时，需要为第二个事件填写当前 revision 和新的 event id；同一个 event id 与相同内容重放会返回之前的结果，不会再修改项目。event id 内容冲突或过期 revision 会拒绝。

## 输入约定与执行保证

每个资产声明 `id / scene / kind / spec`。目前 kind 仅支持 video 和 subtitle。spec 使用 `{{hero.costume}}` 形式的模板；reads、values 的键、模板属性必须完全对应。config 仅用于视频，保存 H3 的 seed、尺寸、帧数、步数等参数。字幕 output 是数据库中的原始文本，尚未生成 SRT、时间戳或烧录字幕。

三类依赖行为：

| 边类型 | 失效传播 | 提交时记录的来源 |
| --- | --- | --- |
| order | 不传播；仅阻塞未完成下游任务的执行顺序 | 不进入内容指纹 |
| state | 编辑属性匹配时传播，更新子资产状态 | 父资产指定属性值 |
| reference | 父资产失效时保守传播 | 父资产 revision、input_hash、output |

state 边必须声明 keys，父子初始值必须一致；与上游 state 矛盾的单点编辑会拒绝。依赖和资产顺序在初始化后固定。依赖标注仍由用户提供，遗漏依赖仍可能产生错误复用。

实际执行固定使用零风险预算，不执行离线规划器未经验证的高风险复用。每次编辑在同一个 `BEGIN IMMEDIATE` 事务中完成版本校验、DAIR 规划、资产失效、任务创建和事件记录。

提交任务前保存：资产 revision、完整 spec、依赖快照、后端地址、实际工作流 JSON、外部 prompt_id 和 SHA-256 输入指纹。完成时从当前输入重建指纹，只允许兼容的结果提交。参数由持久化工作流固定；重启不会以新的命令行参数重提已有任务。

项目所有当前资产均成功、来源校验均通过后，才原子写入该项目 revision 的 Timeline manifest。生成过程中 `status.timeline` 为 null，不返回旧版本假装已完成。历史 manifest 保留在 `vf_timelines`。这里的 Timeline 是资产清单，没有视频拼接、片段时长映射或外部 Draft 写回。

## 参考图和外部副作用边界

运行时会记录 reference 来源并传播失效，但内置 H3 工作流还没有参考素材加载节点绑定。遇到 reference 资产时，CLI 会在提交前输出 validation_error 并保留 pending，绝不会假装消费了参考图。Python 接口可传入 `build(inputs, job_id)`，实现真正使用依赖快照的专用适配器；当前只用 mock 适配器测试该接口。

未知视频任务只查询原 prompt_id，不能证明不存在就不重提。提交前崩溃会留下一个可能从未发出的 unknown 任务；这是当前为了避免重复外部副作用作出的保守选择，需要人工对账。不能仅凭客户端 prompt_id 宣称服务端 exactly-once。

用户继续编辑可以使旧任务 superseded，但不能取消已进入 ComfyUI 的 GPU 工作。旧任务不再写入当前资产；现在仍会按原任务 ID 查询其远端终态，但不会推算其费用或额外下载旧产物。不要把新 revision 当作对未知任务的无风险重试。

资产运行时的 output 仍为 ComfyUI `/view` URL。新增实验入口可下载、校验并归档当前完整版本，见 [EXPERIMENTS.md](EXPERIMENTS.md)；首次下载前 URL 对应文件不被覆盖仍无保证。当前不包含多机队列、任务租约、自动重试策略、GPU 心跳、视觉质量模型或自然语言编辑解析。

## 真实实验仍待完成

当前环境没有接入你的实际 ComfyUI/GPU，本次没有生成真实 H3 视频。新增实验入口已支持归档初始/修改产物、配置、任务 ID 与客户端观测时间；下一步在 H3 服务器采集真实结果，再做独立故事的编辑正确性及一致性盲评。
