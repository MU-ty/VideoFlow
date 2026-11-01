# VideoFlow

VideoFlow 是一个面向视频生成的早期任务运行时。它将 Scene、依赖图、版本和生成任务保存在 SQLite 中，并接入自建 ComfyUI 的 MiniMax H3 工作流。

## 环境

- Python 3.10+，VideoFlow 无第三方 Python 依赖。
- 已启动并可访问的 ComfyUI；内置 H3 图参照原有 ComfyUI 0.42 工作流，需要 `MiniMaxH3ImageToVideo`、`CreateVideo`、`SaveVideo` 等节点及下列模型文件：
  - `models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors`
  - `models/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`
  - `models/vae/minimax_h3_video_vae_int8_convrot.safetensors`
  - `models/vae/minimax_h3_audio_vae_fp32.safetensors`

模型名称或节点不同的环境请使用自定义工作流。运行前用 ComfyUI 确认 H3 工作流可以独立生成；GPU 显存、模型和节点均由 ComfyUI 环境负责。

## 快速开始

```bash
python -m unittest discover -s tests -v
python worker.py --db project.db add S1 'A cinematic rainy city at dusk'
python worker.py --db project.db add S2 'A rooftop chase at dusk'
python worker.py --db project.db depend S1 S2
python worker.py --db project.db --server http://127.0.0.1:8188 run-once
python worker.py --db project.db status
```

`run-once` 每次查询正在执行的任务并提交就绪场景后退出。生成期间请**定期再次执行同一命令**；上游完成后，下一次执行会提交其就绪的下游。`--server` 必须和任务提交时一致；重启后用相同数据库及服务器地址继续。任务完成后 `status` 中的 `asset` 是指向 ComfyUI `/view` 的 URL，不会自动下载；该地址依赖 ComfyUI 可访问及输出文件仍存在。

修改场景并使显式依赖的下游失效：

```bash
python worker.py --db project.db revise S1 'A snowy city at night'
python worker.py --db project.db --server http://127.0.0.1:8188 run-once
```

旧版本仍可能在 ComfyUI 消耗计算资源；旧结果不会写入当前场景。失败任务不会自动重试，确认原因后可用 `revise` 创建新版本。

内置参数可通过 `run-once --help` 查看。例如 `--frames 124 --width 864 --height 480 --steps 20 --seed 42`。帧数限制为 124 到 362、步长 17，宽高必须为 32 的倍数。

## 使用自己的 API 格式工作流

在 ComfyUI 导出 **API 格式** JSON，确保包含保存视频节点。指定接收提示词的节点 ID 和输入字段：

```bash
python worker.py --db project.db --server http://127.0.0.1:8188 run-once --workflow workflow_api.json --prompt-node 5 --prompt-field prompt
```

每次调用都需要提供相同的工作流和参数。适配器仅替换指定输入；模型名称、种子、保存路径等由该 JSON 决定。要求成功历史记录中恰好有一个 `.mp4`、`.webm` 或 `.mov` 输出；没有或有多个时任务标记失败。模板中的保存路径应使用独立前缀，避免输出覆盖。

## 恢复与限制

提交前先在 SQLite 中保存客户端生成的 `prompt_id` 和服务地址；重启后通过 `/history/{prompt_id}` 和 `/queue` 查询。提交响应丢失、记录不在队列和历史等情况保留为未知状态，**不自动重新提交**。请在 ComfyUI 检查后人工决定是否通过 `revise` 创建新任务；ComfyUI 清理历史或重启后可能无法判断此前是否执行。客户端 ID 不代表服务端具备幂等保证，不能保证恰好执行一次。提交前崩溃也可能留下未知任务，需要人工处理。

这是单机原型，不具备多 worker 并发租约、自动重试、认证、模型自动下载、分布式队列、自动拼接或视觉一致性评估。仅连接可信的本地服务或通过 SSH 隧道保护远程服务，不要直接公开未鉴权的 ComfyUI。未连接真实 GPU 进行本次端到端验证；测试使用本地模拟 HTTP 服务。原有 `demo.py` 仍是模拟示例，使用新数据库运行。


## DAIR 增量研究与资产执行

新增按实体属性与依赖类型规划增量重生成的 DAIR 原型，以及可恢复的资产级执行入口。视频与字幕分别版本化，字幕编辑不必重生成视频。

```bash
python dair.py fixtures/example.json
python research/benchmark.py
python research/fault_benchmark.py
python asset_worker.py --db assets.db init fixtures/assets.json
python asset_worker.py --db assets.db run-once --server http://127.0.0.1:8188
```

- [离线算法、基线和证据边界](research/README.md)
- [资产执行、编辑示例和恢复机制](research/EXECUTION.md)

执行路径为结构化编辑 → DAIR → SQLite 版本化资产任务 → 本地字幕 / ComfyUI H3 → 指纹校验 → 原子 Timeline manifest。旧 `worker.py` 仍执行保守 DAG 失效，使用新入口才能执行 DAIR 计划。

当前 32 项测试通过，包括本地模拟 HTTP 集成。真实 GPU 视频质量与成本尚未验证；字幕目前是文本资产；内置 H3 遇到 reference 依赖会拒绝提交，等待真正的参考加载适配器。
