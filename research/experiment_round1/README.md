# VideoFlow v3.8 补实验

## 本地核验

| 证据 | 覆盖范围 | 结果 |
| --- | --- | --- |
| 工作流对照模拟 | 48 次主矩阵运行、2 次灵敏度运行 | 24 个默认配置配对在模拟中打平。18 秒遮蔽下，固定轮询加 20 秒宽限的对照比 VideoFlow 少一次接受 |
| 生成引用集成 | 2 个拓扑 × 3 个规划方法，使用本地 HTTP 测试后端和合成视频 | 验证上传、SHA-256 回读、下游首帧绑定、旧结果拒绝、独立镜头复用和整版发布 |
| 新实验的真实 GPU 数据 | 待 worker-0 运行 | 模拟结果和测试素材只用于核验程序 |


## 在 worker-0 跑两个真实 pilot

拉取 `research/experiment-round1` 分支后进入 VideoFlow 仓库根目录。要求 Python 3.10+、ffmpeg、ffprobe、已加载 H3 权重的 ComfyUI 和独占的空闲队列。以下命令按顺序运行。每条命令使用新的输出目录。

```bash
curl -fsS http://127.0.0.1:8188/system_stats >/dev/null

python -u research/recovery_observability_comfy.py \
  --suite workflow --pilot --server http://127.0.0.1:8188 \
  --output runs/workflow-pilot-01

python -u research/recovery_reference_comfy.py \
  --story chain --methods dair --supersede --server http://127.0.0.1:8188 \
  --output runs/reference-pilot-01
```

第一条 Python 命令跑一组持续遮蔽配对。第二条跑五个镜头并验证连续编辑的版本 1 和版本 3。版本 2 的 S1 必须已进入后端，版本 3 才提交编辑。程序会检查旧 S1 确实在后端成功、未在账本提交且版本 2 未发布。H3 节点能力通过服务器的 object_info 在运行前确认。上游视频的第一帧通过 FFmpeg 提取，按内容哈希上传并回读验证，然后接入下游 H3 的 first_frame。另有独立 S5 作为复用对照。

## 正式矩阵

| 批次 | 规模 | 问题 |
| --- | --- | --- |
| workflow | 40 次 | 独立镜头和带顺序边的故事，在短暂及持续遮蔽下，强对照能否复现局部恢复 |
| workflow_capacity | 32 次 | 同一镜头轨迹在 cap 2 与 4 时，两种策略如何受容量约束 |
| workflow-sensitivity | 24 次 | 把遮蔽改成 18 秒，基线固定 5 秒轮询、20 秒宽限，检查对触发时机的敏感度 |
| reference-chain 与 reference-fanout | 共 12 次 | 真实生成引用的链与扇出在同种子下，DAIR、属性 Worklist 和全量重生成的复用与执行差异 |

```bash
python -u research/recovery_observability_comfy.py \
  --suite workflow --repetitions 5 --output runs/workflow-main-01

python -u research/recovery_observability_comfy.py \
  --suite workflow_capacity --repetitions 4 --output runs/workflow-capacity-01

python -u research/recovery_observability_comfy.py \
  --suite workflow --repetitions 3 --workflow-mask-seconds 18 \
  --workflow-grace-seconds 20 --workflow-fixed-poll \
  --output runs/workflow-sensitivity-01

for story in chain fanout; do
  python -u research/recovery_reference_comfy.py \
    --story "$story" --methods dair attr_dag full --repetitions 2 \
    --supersede --output "runs/reference-${story}-01" || break
done

for batch in workflow-main-01 workflow-capacity-01 workflow-sensitivity-01; do
  python research/summarize_workflow.py "runs/$batch" --output "runs/$batch/paired.json"
done
```

每组中的方法共用种子，偶数组反转运行顺序。需要拆成多批时，用 `--start-repetition` 指定还未完成的重复编号，并换输出目录。中断时保留已有 protocol.json、summary.json 和每次试验目录。每个完整配对单独报告延迟差；单侧超时单独计入发布率。独立的上游审计可以在客户端截止后继续收集终止历史，此阶段不再提交任务或发布。后端报错、初始队列非空或试验起点不成立时保留错误和账本，单独标明该试验的协议状态。

工作流对照使用独立的属性 Worklist 和显式镜头指纹对账，沿用共享 SQLite 持久化账本、提交保护、容量计算及局部替换事务。默认两组均为指数退避，比较从首个 Unknown 计起的 15 秒宽限与三次 Unknown 阈值。这个默认对照刻意匹配触发时机，可作为策略能否被复现的检验。灵敏度批次同时改变基线轮询与宽限，单独解释。这里比较的是共享运行时内的工作流策略，尚无 Temporal 或 ExoFlow 的实测。

引用实验以实际提交的工作流、上下游产物哈希、Manifest 以及 S5 是否复用为通过条件。保留原始视频用于后续人工检查目标道具颜色和白色条纹。哈希核验只能证明素材来源和复用，颜色和条纹检查需要实际画面。

## 本地复核

```bash
python research/smoke_grace.py
python research/smoke_experiments.py smoke_results.json
python research/audit_archived_cost.py /path/to/v3.8/evidence --output archived_cost_results.json
```

第一、二条无需 GPU，第二条需要 ffmpeg 和 ffprobe。第三条读取原 v3.8 复现包里的证据压缩档，需要本地持有该复现包。
