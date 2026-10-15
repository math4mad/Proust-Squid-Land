# Proust-Squid Kaggle 实验战报（首飞）

- kernel：`math4amd/proust-squid-prealign-geometry-speed` v1
- status=COMPLETE，elapsed **731.1s**；15 条件 × 3000 步 × seed 0，GPU
- 产物（正式，已落盘本项目，加 `kaggle_` 前缀与 200 步短验版区分）：`results/kaggle_final_results.csv`（15×11）、`results/kaggle_partial_results.csv`、`results/kaggle_curves.png`（92781B，2700×750，PNG 完整——IEND 在位、无截断）
- 回收：`kaggle kernels output math4amd/proust-squid-prealign-geometry-speed`（首次 tail 截断误判产物缺失，全量下载齐；curves.png 完整性经字节级校验）
- 本地短验产物（200 步，历史记录）：`results/final_results.csv`、`results/partial_results.csv`、`results/curves.png`

## 结果（命令：`pandas.read_csv(final_results.csv)`）

**α 扫描（预对齐程度 0→1）**

| α | final_acc | steps_to_0.5 | geo_form_step | final_sr | align |
|---|---|---|---|---|---|
| 0 | 0.795 | 500 | NaN | 23.88 | 0.320 |
| 1/6 | 0.818 | 407 | NaN | 13.15 | 0.358 |
| 2/6 | 0.864 | 476 | 250 | 6.91 | 0.409 |
| **3/6** | **0.886** | 500 | 250 | 5.02 | 0.460 |
| 4/6 | 0.841 | 484 | 250 | 4.38 | 0.479 |
| 5/6 | 0.477 | 416 | 250 | 4.08 | 0.489 |
| 1 | 0.341 | 428 | 250 | 3.90 | 0.491 |

**谱截断 k 扫描**：k=2: 0.659 / k=4: **0.773** / k=8: 0.636 / k=16: 0.523 /
k=32: 0.409 / k=64: 0.341（k 单调增大 acc 单调下降）。

**对照**：perm_ctrl acc=0.386、auc=0.084（置换破坏结构，auc 崩，对照有效）；
scale_ctrl 与 α=1 数值全同（acc 0.341 / sr 3.896）——scale 对照与全注入等价，对照设计缺陷。

## 判读

1. **预对齐程度与能力呈倒 U 形**：α=0.5 峰值 0.886；全注入 α=1 崩至 0.341
   （无预对齐 α=0 为 0.795）。**「预对齐越多越好」在义素图上不成立**
   ——与原 path-star 结论（C3 快 ~50×）相反：全注入 DeepWalk 反而把模型
   锁定在嵌入几何里，损害义素组合学习。
2. **谱截断同形**：k=4 峰值 0.773，k=64（全谱）最差 0.341——与 α 扫描
   一致，指向「结构化但适度低维」的注入最好。k=64 与 α=1 数值近似相同
   （sr 3.873 vs 3.896），因 DeepWalk 嵌入有效秩低，全谱投影 ≈ 全嵌入。
3. **steps_to_0.5 分辨率不足**：EVAL_EVERY=250（占 3000 步的 8.3%），
   α≥1/3 全部 ≤500 步（首次/二次评估即达标），无法区分 407/476/500 的
   真实差异。**几何速度结论暂不能下**——次飞需 EVAL_EVERY ≤ 50。
4. **geo_form_step 阈值 5.0 太松**：所有 DeepWalk 注入条件首次评估即
   sr<5（=250），α=0/α=1/6 全程未达（NaN，终态 23.88/13.15）。阈值应对齐
   夜航口径或收紧（如 3.0），否则该指标在预对齐条件下是构造伪影。
5. **终态 sr 与 α 单调递减**（23.88→3.90）但 acc 倒 U——**秩低 ≠ 学得好**：
   α=1 的 sr 最低（3.90）却 acc 最差（0.341）。stable_rank 是结构指标，
   不是能力指标（与夜航「stable_rank 仅记录」口径一致）。

## 图口径（与夜航版的关系）

- 本实验图 = **Proust-Squid 版**：单维 P4 链 + 共享根，边 = 链内值差恰 1，
  度 ≤9、直径 12。hop-k = 沿单维值差 k。
- 夜航 C1 图 = **Hamming 图 H(4,4)**：边 = 汉明距离恰 1（值差任意均 1 跳），
  度 12、直径 4、无根。hop-k = k 个维不同。
- **两图 hop 语义不可互译**；本实验结论不直接外推到夜航图——ima 移交摘要
  已列待办：C2（随机冻结）/ C3（DeepWalk 预对齐）在夜航 Hamming 图上重跑。

## 待办（次飞）

- [ ] EVAL_EVERY ≤ 50（steps_to_0.5 分辨率）
- [ ] GEO_THRESHOLD 收紧（5.0 → 3.0）或对齐夜航口径
- [ ] scale 对照修正（当前与 α=1 等价）
- [ ] 多种子（当前 seed 0 单点）
- [ ] C2/C3 在夜航 Hamming 图上跑（见 ima 移交摘要）

## 次飞修复清单（2026-10-15 冻结，先于执行）

首飞五项待办逐项落实为可执行规格（**判据先冻，扫完只填数**）：

| # | 修复 | 首飞病灶（实测） | 次飞规格 |
|---|------|------------------|----------|
| 1 | **EVAL_EVERY ≤ 50** | 250/3000 = 8.3% 分辨率：α≥1/3 全部 ≤500 步达标，407/476/500 不可区分，**几何速度结论暂不能下** | `EVAL_EVERY = 50`（60 个评估点，分辨率 1.7%）；steps_to_0.5 / steps_to_0.9 结论方可信 |
| 2 | **GEO_THRESHOLD 收紧** | 5.0 太松：所有 DeepWalk 注入条件首次评估即 sr<5（=250），α=0/α=1/6 全程未达（NaN）——该指标在预对齐条件下是**构造伪影** | `GEO_THRESHOLD = 3.0`（对齐夜航 pe_norm 警戒 3.0 口径）；geo_form_step 重新有判别力 |
| 3 | **scale 对照修正** | `conds["scale_ctrl"] = {"mode":"scale","factor":1.0}` 而 `make_init` 的 `mode=="scale"` 分支 = `deepwalk_emb.copy()` —— **与 α=1 完全等价，非对照**（首飞「scale 与 α=1 等价」即此 bug 的现场） | scale_ctrl 修为**同范数随机嵌入**：`rnd · (mean‖DW‖ / mean‖rnd‖)`（结构无、尺度同）——才是「尺度 vs 结构」的干净对照；α=1 与 scale_ctrl 必须分开 |
| 4 | **多种子** | `SEEDS = [0]` 单点（`kaggle_experiment.py:413`） | `SEEDS = [42, 43, 44]`（与夜航同种子族）；结果表加 mean/min/max 列 |
| 5 | ~~C2/C3 夜航 Hamming 重跑~~ | 首飞待办 | **已完成（2026-10-15 夜航第二飞）**：C2 随机冻结 hardest .307（较 C1 .583 跌 47%）、C3 DeepWalk 预对齐 hardest .995 / hop 2–6 对齐 .658 ≥ .40 推翻线（`Concept-Space-Sphere/experiments/seme-hypercube/results/{C2,C3}/`） |

**口径声明（次飞）**：
- 图口径不变（P4 单维链，与夜航 Hamming H(4,4) hop 语义不可互译——次飞结论仍不外推夜航图）；
- TRAIN_STEPS = 3000、stage0 形制模型（4 层 d192）不变——修的是**测量分辨率与对照纯度**，不是能力上限；
- α 七档（`np.linspace(0,1,7)`）不变；α=0 为 0.02 缩放随机初始化（本仓口径，与夜航 α 扫描的 N(0,1) 锚定不同，见夜航 prereg §7 审阅者注）。
- **新增观察项**（承接首飞判读⑤「秩低 ≠ 学得好」）：每条件记录终态 stable_rank 与 hardest 的散点，检验倒 U 形是否独立于 sr 单调递减。

**回收四验**（夜航纪律移植）：完赛 + 15 条件格数齐 + 哨兵专属词 + 无 0 字节壳。
