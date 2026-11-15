# VideoFlow 在线端到端重着色对照

固定两个新种子，Teapot-Chain 92023 与 Toolbox-Fanout 92033。每个种子生成一次六镜头 Rev.1，三种策略共享同一初始项目，对同一个锚点各执行一次 scarlet red 编辑。S3 是预设遗漏的同物体镜头，S4、S6 是它的引用后继，S2 是不同身份的干扰物，S5 是独立镜头。保留两个预定种子的结果，源素材盲评记录标记、部件可见性和身份。

| 策略 | 依赖与渲染 |
| --- | --- |
| EGDR | 提交编辑后从冻结的源视频提取证据帧，调用 Ollama，消费当前证据并发布 |
| Declared Worklist | 沿已有依赖图工作，无视觉验证 |
| Full | 预先给定正确的直接重着色目标，整图重生成 |

EGDR 每个种子在线独立取证，证据完成后由 worker 在下一轮轮询应用。所有策略的 S1、S3 由提示词驱动生成。S4、S6 的 H3 first_frame 自动从已提交的上游视频解码、上传并校验哈希。无需手绘蒙版或准备目标首帧。这是已有故事模板上的新种子验证，结论按两个配对种子报告。

## GPU 节点运行

需要 Python 3.10+、FFmpeg/FFprobe、空闲专用 ComfyUI、H3 工作流和 Ollama 中的 qwen2.5vl:7b-ctx8k。默认地址为 127.0.0.1:8188 与 127.0.0.1:11434。可以用 --server 和 --ollama 指定地址。

~~~bash
export PATH="$HOME/apps/videoflow-ffmpeg/ffmpeg-7.0.2-amd64-static:$PATH"
run_dir="runs/egdr-online-e2e-$(date +%Y%m%d-%H%M%S)"
python3 -u VideoFlow_Online_E2E/run_online.py --output "$run_dir" &&
python3 VideoFlow_Online_E2E/export_blind.py "$run_dir" --out "${run_dir}-blind"
~~~

--prepare-only 可离线验证两块清单。只跑一个故事时指定 --stories teapot-chain 或 --stories toolbox-fanout，并使用新的输出目录。运行失败时，根目录 report.json 保留已完成的组、失败原因和仍待运行的种子。

盲评包只导出首次发布的 Rev.2 视频。仅将 send_to_reviewers.zip 交给评审。两位评审独立提交并分别保存原始评分 CSV，私有映射只供汇总使用。先检查 Rev.1 的 source_screen.csv，再查看匿名输出填写 ratings.csv。目标不可见填 unclear 并保留在分母中，非目标的重着色字段填 NA。

## 三类结果

1. **依赖发现**。仅 EGDR 报告 S3 是否得到正向匹配、是否因回退进入计划，以及是否误选 S2。结构化确认、首阶段正向保留和未知回退可从各阶段状态区分。
2. **重着色**。策略盲评逐段检查目标部件在可见帧中是否保持指定颜色，并记录身份、时间一致性和非预期变化。评分单位是片段，配对实验单位是故事种子。
3. **首次发布**。逐种子报告从提交编辑到首次 Rev.2 Manifest 的墙钟时间、缺失与额外重生成的镜头、源视频是否逐字节保留、引用谱系和服务端接受数。EGDR 的墙钟时间包含在线证据帧提取、推理、应用与渲染。Rev.1 素材准备在计时之前。实际 GPU 时间未测量。

~~~bash
python3 VideoFlow_Online_E2E/summarize.py "$run_dir/report.json"
python3 VideoFlow_Online_E2E/summarize.py "$run_dir/report.json" \
  --key "${run_dir}-blind/KEEP_PRIVATE_key.json" --ratings /path/to/filled-ratings.csv \
  > "$run_dir/first-publication-reviewed.json"
~~~

每份评分记录单独汇总。未提供评分时，重着色结果为 null。
