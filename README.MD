# 普鲁斯特与乌贼的启示

> 预对齐程度 → 几何速度：义素超立方体图 + PathStarTransformer 实验

## 概述

本项目基于 Maryanne Wolf《Proust and the Squid》中关于阅读双通路的启示，在义素超立方体图（seme hypercube graph）上训练 `PathStarTransformer`，研究**预对齐程度如何影响几何形成的速度**。

核心假设：在义素链任务上，DeepWalk 预对齐编码了图结构信息，可以加速 Transformer 注意力几何的形成——体现为更快的 `stable_rank` 下降和 `hardest_token_acc` 上升。

## 项目结构

```
src/
  seme_graph.py      义素超立方体图构造（适配原仓库 graph.py 接口，含虚拟根 v_root）
  seme_deepwalk.py   DeepWalk 预对齐嵌入（gensim 优先，numpy 回退）
  model.py           PathStarTransformer（来自 llm-geometry-analysis 原仓库）
  metrics.py         6 个几何指标（hardest_token_acc / stable_rank / embedding_auc / alignment_delta / translation_consistency / eigenspace_gap）
  train.py           训练循环 + 15 个预对齐条件（7 α + 6 k + 2 对照）
  data.py            数据加载
  config.py          配置
  graph.py           原始图构造
  deepwalk.py        原始 DeepWalk
tests/
  smoke_test.py      本地冒烟测试
kaggle_experiment.py Kaggle 完整可运行脚本（单文件，clone 原仓库后直接跑）
results/
  smoke_test_curves.png
  smoke_test_results.json
```

## 本地冒烟测试

```bash
pip install -r requirements.txt
python tests/smoke_test.py
```

冒烟测试使用小规模配置（d_model=64, steps=3000）验证整条管线（2026-10-14 复跑）：

- **DeepWalk hop 校验 PASS**（hop-1 平均余弦 +0.179 vs 远端 +0.068，差 +0.111 > 0.05）→ 嵌入真编码义素图结构
- 训练有效：预对齐 acc 0.000→0.182（best 0.205），随机 0.000→0.432；`stable_rank` 下降可检测
- 全部指标函数正常工作

### 诊断记录（2026-10-14，冒烟 500 步版异常的根因）

1. **arm_cluster_acc=0.201 是算术必然，非 DeepWalk 质量问题**：义素超立方体的节点被
   4 条链共享（root 被 256 条共享），平均每节点属 4.98 条臂；原仓库
   `validate_pretrained_embeddings` 的单标签 argmax 期望 acc = 1/4.98 = 0.201。
   该度量为单臂 path-star 设计，在义素图上不按字面判读——义素图适用的校验是
   `validate_deepwalk_hop`（按图跳数分层余弦相似度，已入 tests/smoke_test.py）。
2. **500 步 acc 全 0 是步数不足**：path-star 基线需 ~954 步方达 50% hardest acc，
   义素图更难；冒烟已加长到 3000 步（预对齐 0.182 / 随机 0.432）。
3. **预对齐 α=1 在义素图上未见加速**（3000 步：0.182 < 随机 0.432）——与原
   path-star 结论（C3 快 ~50×）相反，正是本实验「预对齐程度 → 几何速度」要回答的
   科学问题；α 扫描（0→1 七档）会给出单调性证据。
4. **spectral 条件的 geo_form_step 是构造伪影**：谱截断投影本身把嵌入压到低秩
   （初始 sr ≈ k），其「几何成形步数」恒为首次评估步，跨条件不可比——
   判读时只比 alpha/scale/perm 条件的 geo_form_step。
5. **auc / alignment_delta 同样为单臂度量**（义素图节点共享多臂），仅作记录。

## Kaggle 实验

`kaggle_experiment.py` 是自包含单文件脚本（Kaggle 舱内自动 git clone 公开原仓库
rsharma441/llm-geometry-analysis，本机回退本地克隆）：

- kernel：**math4amd/proust-squid-prealign-geometry-speed**（GPU，v1 起）
- 15 条件：α 插值 7 档（0→1）+ 谱截断 k ∈ {2,4,8,16,32,64} + scale/perm 对照
- 输出：`results/final_results.csv` + `results/partial_results.csv`（逐条件即算即落盘）
      + `results/curves.png`
- 能力判据 = hardest_token_acc / steps_to_0.5 / stable_rank；auc/align 仅记录（见诊断 5）



## 预对齐条件矩阵

| 条件 | 类型 | 说明 |
|------|------|------|
| alpha_0 ~ alpha_1e-3 | α-blend | DeepWalk 嵌入与随机嵌入按不同比例混合 |
| k_2 ~ k_64 | 谱截断 | 对 DeepWalk 嵌入做 SVD 截断到 top-k 维 |
| deepwalk_only | 对照 | 纯 DeepWalk 嵌入 |
| random_only | 对照 | 纯随机嵌入 |

## 关联仓库

- 原始几何分析框架：[llm-geometry-analysis](https://github.com/math4mad/llm-geometry-analysis)
- 实验对话：`external/dialogue.md`

## 依赖

```
torch>=2.0
numpy>=1.20
scipy>=1.7
matplotlib>=3.5
tqdm>=4.60
gensim>=4.0  (可选，DeepWalk 质量更好)
```
