# 两故事、十二候选视频：联合选择空间小试验

本轮只检查固定候选池内是否存在人工可察觉的选择差距，不开发或声称验证了新的联合选择、预算分配算法。没有正结果时保留负结果并暂停。

## 固定设计

- 选择旧 `v8-content-pilot-01` 的温室和港口故事。
- 每个故事 S2/S4/S6 各两个不同种子，12 个新视频任务；源镜头各自原 seed 与原 seed+1000003。
- 同一镜头的两个候选使用完全相同的提示词和 H3 设置：864×480、124 帧、20 步；纯文本条件，没有参考图、首帧或新模型。
- 提示词在原蓝色修改要求上明确加入共同外形规格，要求三个镜头呈现同一浇水壶/背包。新任务比旧实验增加了明确跨镜头要求，不能将此前可接受的视频重新标为失败。
- 原 S1/S3/S5 从 revision-1 归档按 SHA-256 验证后复制。它们是场景上下文，没有目标产品身份参考，因此本试验不能证明“保持同一原始商品身份”。
- 生成顺序、匿名映射与评分规则在提交前冻结于 plan.json；两种选择策略共用全部候选。
- 与过去实验相同，复用已有版本化执行、原外部任务 ID 查询和未知提交不自动重试机制。每个候选最多一次提交意图；崩溃发生在持久化意图和真正 POST 之间时可能无法自动完成，保守保留 unknown，而不是重复花费。
- 上限两个远端未终结任务。不得将本轮代码或模型配置变更后继续写入同一输出目录。

## 服务器执行

以下假定旧源目录为 `~/VideoFlow/runs/v8-content-pilot-01`。它需要包含 `greenhouse-seed-42/dair/configuration.json`、`harbor-seed-42/dair/configuration.json` 以及各自 `artifacts/revision-1.json` 和其中列出的文件。可以把 `--source-root` 指向这两块的共同上级目录。路径缺失或锚点校验不通过时，在提交任何 GPU 任务前报错。

```bash
cd ~/VideoFlow
git fetch origin research/joint-pool-v1
git worktree add ~/VideoFlow-joint-pool-v1 FETCH_HEAD
cd ~/VideoFlow-joint-pool-v1

python -u research/run_joint_pool.py generate \
  --source-root ~/VideoFlow/runs/v8-content-pilot-01 \
  --output runs/joint-pool-v1-real-01 \
  --server http://127.0.0.1:8188 \
  --timeout-seconds 21600
```

仅准备、不执行生成：将 `generate` 换成 `prepare`，去掉 server 和 timeout 参数。中断后进入新 worktree，重复同一条 `generate` 命令；不需要再次创建 worktree。提交失败或 unknown 请保留目录和 status.json，不要删除数据库重新跑。

输出包括 plan.json、execution.json、project.db、events.jsonl、status.json、provider 原始检查、视频归档与 review.zip。GPU 时间、语义成功率在没有测量或评分前保持 null。

## 完成后的两阶段盲评

1. 将 `review.zip` 下载到本机，完整解压再用浏览器打开。不能只复制 HTML，因为视频位于 media 子目录。
2. 单镜头评者打开 individual.html，完整看 12 段，判断目标蓝色/该镜头要求是否实现，并给单镜头质量 1–5 分。导出 individual_ratings.json。
3. 另一位未见上述评分的评者打开 joint.html，评价每个故事的全部 8 组，合计 16 组。评价同一产品的核心外形、场景和动作逻辑；正常视角变化不算错误。需要时展开保留场景镜头。导出 joint_ratings.json。
4. 默认静音，只评视觉。页面不会验证观看完成；在实验记录中自行注明完整观看还是抽样。页面尝试本地保存进度，但仍应保留导出的 JSON。
5. 在评前不要向评者提供 plan.json、private/review_key.json 或分析结果。使用同一评者做两阶段时报告这一局限，不称为独立盲评。两个人、两个故事也不能给出总体统计结论。

独立选择规则：每个镜头从 `edit_success=yes` 的候选中挑单镜头分数最高者；同分取匿名 ID 较小者。某镜头没有通过候选则弃权。

整组事后最佳：从三个候选都通过单镜头修改检查的组合中，挑整组分数最高者；同分取组合 ID 较小者。它使用整组人工评分作为未来信息，只估计候选池优化空间，不能当作线上算法成绩。

```bash
python research/joint_pool_review.py analyze \
  --output runs/joint-pool-v1-real-01 \
  --individual /path/to/individual_ratings.json \
  --joint /path/to/joint_ratings.json
```

分析拒绝缺失、重复、不同 study_hash 或非法评分，重新核验 review 视频哈希。评分输出文件应保留原始记录，不能根据结果改分后覆盖原版本。

## 预先声明的探索性门槛

仅当两个故事中“整组事后最佳 − 独立选择”的整组评分都至少为 1 分（五分制），且两个最佳组合都被认为可接受，才记录为有描述性信号。否则暂停本候选；不在这两故事上调分数或换规则找正结果。

即使有信号，也只能进入独立复核和新故事重复阶段；不能称为稳定优势或显著提升。后续方法必须与同候选池的联合穷举上界、独立选择和顺序贪心比较，并在新候选池/新故事测试。当前没有测试动态预算分配。
