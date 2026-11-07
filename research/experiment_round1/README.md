# VideoFlow v3.8 补实验

## 本地核验

| 证据 | 覆盖范围 | 结果 |
| --- | --- | --- |
| 工作流对照模拟 | 48 次主矩阵运行、2 次灵敏度运行 | 24 个默认配置配对在模拟中打平。18 秒遮蔽下，固定轮询加 20 秒宽限的对照比 VideoFlow 少一次接受 |
| 生成引用集成 | 2 个拓扑 × 3 个规划方法，使用本地 HTTP 测试后端和合成视频 | 验证上传、SHA-256 回读、下游首帧绑定、旧结果拒绝、独立镜头复用和整版发布 |
| 新实验的真实 GPU 数据 | 待 worker-0 运行 | 模拟结果和测试素材只用于核验程序 |


## 第一阶段：在 worker-0 跑一个真实配对 pilot

拉取 `research/workflow-pair-pilot` 后从 VideoFlow 仓库根目录执行。
要求 Python 3.10+、已加载 H3 权重的 ComfyUI 和独占的空闲队列。
每次试验使用新的输出目录。

```bash
curl -fsS http://127.0.0.1:8188/system_stats >/dev/null

python -u research/recovery_observability_comfy.py \
  --suite workflow --workflow-story independent --pilot \
  --server http://127.0.0.1:8188 --output runs/workflow-pair-pilot-01 &&
python research/summarize_workflow.py runs/workflow-pair-pilot-01 \
  --output runs/workflow-pair-pilot-01/paired.json
```

这两次运行共用持续遮蔽轨迹。汇总会拒绝失效集合、首轮在途镜头、
可用提交槽位或首个 S4 遮蔽观测不一致的配对。通过时，报告中的
`boundary` 应显示只失效 S2、S2/S4/S6 均在途、首轮可用槽位为 0，
且首个 S4 探测被遮蔽。之后每轮的可用槽位和实际遮蔽观测记录在
`worker.jsonl`，用于解释不同策略执行后的容量变化。

## 第二阶段：五个配对种子

Pilot 的配对边界通过后再执行。正式批次在独立镜头轨迹上运行
5 种子 × 2 遮蔽 × 2 策略，共 20 次，采用 12 秒短暂遮蔽和持续遮蔽。

```bash
python -u research/recovery_observability_comfy.py \
  --suite workflow --workflow-story independent --repetitions 5 \
  --server http://127.0.0.1:8188 --output runs/workflow-paired5-01 &&
python research/summarize_workflow.py runs/workflow-paired5-01 \
  --output runs/workflow-paired5-01/paired.json
```

按遮蔽条件报告正确发布率、仅在双方均按时发布时计算的逐对延迟差，
以及原 S4 首次接受之外的重复接受数。S2 依编辑产生的必要重生成不计为
重复接受。保留每次运行的 `boundary.json`、`report.json`、`worker.jsonl`
和 SQLite 账本。若两种策略的结果接近，就将论文结论限定在连续编辑下的
镜头复用与安全发布机制。若有稳定差异，再用替换排队、后端接受和 Manifest
提交记录定位原因。这里的强基线是工作流策略对照。

## 可选：真实生成引用链 pilot

主对照完成且 GPU 有余量时执行。此命令另需 ffmpeg 和 ffprobe。

```bash
python -u research/recovery_reference_comfy.py \
  --story chain --methods dair --supersede --server http://127.0.0.1:8188 \
  --output runs/reference-pilot-01
```

实际运行的链是 S1→S2→S3→S4，S5 独立。上游已提交视频的第一帧
通过 FFmpeg 提取，按内容哈希上传，回读验证后绑定到下游 H3 的
`first_frame`。查看 `report.json` 中各条引用边的前后哈希、
`changed_reference_frame_edges`、`independent_reused` 和
`stale_result_rejected`。连续编辑时，脚本要求版本 2 的 S1
已进入后端后才提交版本 3，并核对旧结果在后端成功却未提交到版本 3。

## 可选扩展矩阵

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

## v3.9 后续：真实引用链复核

从 `research/reference-evidence` 运行。先复核已有试验并导出真实配图素材，
这一步读取已有文件，无需 GPU。

```bash
export PATH="$HOME/apps/videoflow-ffmpeg:$PATH"
python research/audit_reference_case.py runs/reference-chain-pilot-01/01-dair \
  --output runs/reference-chain-pilot-01/audit.json \
  --frames runs/reference-chain-pilot-01/figure-frames &&
tar -czf "$HOME/videoflow-figure4.tar.gz" -C runs/reference-chain-pilot-01 \
  audit.json figure-frames
```

配图用 revision 1 和 revision 3 的真实输入与输出，标出三条参考帧边及 S5
复用。输入 PNG 是原始上传字节。输出 PNG 是归档视频在 0.2 秒处的样本。
已有单种子案例单列。

先固定链结构和两次编辑触发条件，再运行两组配对种子 3107、7200。
比较 DAIR、属性 Worklist 和全量失效，共六次真实运行，第二组反转方法顺序。
按种子对照引用来源、独立镜头复用、旧结果拒收、提交意图和发布时间。

```bash
python -u research/recovery_reference_comfy.py --story chain \
  --methods dair attr_dag full --supersede --repetitions 2 \
  --server http://127.0.0.1:8188 --output runs/reference-chain-paired2-01
```

现有 20 次强对照可单独做原始记录审计。Provider 执行跨度含主机工作。
接受应答到开始执行是带符号的排队代理量，立即开始时可能为负。

```bash
python research/audit_workflow_run.py runs/workflow-paired5-01 \
  --output runs/workflow-paired5-01/raw-audit.json
```
