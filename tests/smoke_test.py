"""
本地冒烟测试 — 复用原仓库代码 + 义素超立方体适配层。

验证整条管线：
  义素图构造 → DeepWalk → 预对齐条件 → 训练 → 几何指标

用法：python tests/smoke_test.py
"""

import os
import sys
import json

# 把原仓库加入 path（在本地 src 之前，这样原仓库的 src.model 等先被加载）
REPO_PATH = "/Users/mac/Documents/GitHub/llm-geometry-analysis"
PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import torch
import torch.nn.functional as F
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# 先导入原仓库的 src 模块
sys.path.insert(0, REPO_PATH)
from src.model import PathStarTransformer  # noqa: E402
from src.metrics import (  # noqa: E402
    eval_hardest_token_acc,
    eval_embedding_auc,
    eval_alignment_delta,
    compute_stable_rank,
    compute_translation_consistency,
)
from src.train import get_lr  # noqa: E402
from src.data import build_dataset, MixedBatchSampler  # noqa: E402

# 义素适配层：通过文件路径直接导入，避免 src 包名冲突
import importlib.util
def _import_from_file(name, filepath):
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

seme_graph_mod = _import_from_file("seme_graph", os.path.join(PROJECT_PATH, "src", "seme_graph.py"))
seme_deepwalk_mod = _import_from_file("seme_deepwalk", os.path.join(PROJECT_PATH, "src", "seme_deepwalk.py"))

build_seme_hypercube = seme_graph_mod.build_seme_hypercube
build_vocab_seme = seme_graph_mod.build_vocab_seme
build_train_test_leaves_seme = seme_graph_mod.build_train_test_leaves_seme
run_deepwalk_seme = seme_deepwalk_mod.run_deepwalk_seme
validate_pretrained_embeddings_seme = seme_deepwalk_mod.validate_pretrained_embeddings_seme


def validate_deepwalk_hop(emb_matrix, vocab, graph, n_samples=3000, maxd=6):
    """义素图适用的 DeepWalk 校验：按图跳数分层的嵌入余弦相似度。

    为什么需要它：validate_pretrained_embeddings_seme 的 arm_cluster_acc 为
    单臂 path-star 设计（每节点只属一条臂）；义素超立方体的节点被 4 条链
    共享（root 被 256 条共享），单标签 argmax 的期望 acc ≈ 1/4.98 = 0.201
    ——与嵌入质量无关（2026-10-14 诊断实测 0.201，恰为算术必然）。

    义素图语义：相邻节点（hop-1）嵌入应相似，距离越远相似度越低。
    判定：hop-1 平均余弦相似度显著高于远端（hop-6+）且差 > 0.05。
    """
    from collections import deque
    adj = {}
    for u, v in graph["edges_forward"] + graph["edges_reverse"]:
        adj.setdefault(u, set()).add(v)
        adj.setdefault(v, set()).add(u)

    def hop_dist(a, b):
        if a == b:
            return 0
        seen, q = {a}, deque([(a, 0)])
        while q:
            cur, d = q.popleft()
            if d >= maxd:
                break
            for nb in adj.get(cur, ()):
                if nb == b:
                    return d + 1
                if nb not in seen:
                    seen.add(nb)
                    q.append((nb, d + 1))
        return maxd  # 截断视为远端

    node_ids = [vocab["token2id"][t] for t in graph["node_tokens"]]
    E = emb_matrix[node_ids].copy()
    norms = np.linalg.norm(E, axis=1, keepdims=True)
    E = E / np.maximum(norms, 1e-12)

    toks = graph["node_tokens"]
    rng = np.random.default_rng(0)
    by_hop = {h: [] for h in range(1, maxd + 1)}
    for _ in range(n_samples):
        i, j = rng.integers(0, len(toks), 2)
        if i == j:
            continue
        by_hop[min(hop_dist(toks[i], toks[j]), maxd)].append(float(E[i] @ E[j]))

    mean_by_hop = {h: (float(np.mean(v)) if v else None) for h, v in by_hop.items()}
    # 随机嵌入对照（同分布采样）
    R = rng.standard_normal((len(toks), E.shape[1])).astype(np.float32)
    Rn = np.linalg.norm(R, axis=1, keepdims=True)
    R = R / np.maximum(Rn, 1e-12)
    rand_hop1 = []
    for _ in range(n_samples):
        i, j = rng.integers(0, len(toks), 2)
        if i == j:
            continue
        if hop_dist(toks[i], toks[j]) == 1:
            rand_hop1.append(float(R[i] @ R[j]))

    h1 = mean_by_hop[1]
    far = [m for h, m in mean_by_hop.items() if h >= maxd and m is not None]
    far_m = float(np.mean(far)) if far else None
    delta = (h1 - far_m) if (h1 is not None and far_m is not None) else None
    passed = delta is not None and delta > 0.05
    return {
        "mean_cos_by_hop": mean_by_hop,
        "hop1_minus_far": delta,
        "random_hop1_mean": float(np.mean(rand_hop1)) if rand_hop1 else None,
        "threshold": 0.05,
        "pass": bool(passed),
    }


def smoke_test():
    print("=" * 60)
    print("本地冒烟测试（复用原仓库 + 义素超立方体适配）")
    print("=" * 60)

    device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"[1] device = {device}")

    # ── 构造义素超立方体 ──
    n_semes, n_values = 4, 4
    graph = build_seme_hypercube(n_semes, n_values)
    vocab = build_vocab_seme(n_semes, n_values, graph)
    train_leaves, test_leaves = build_train_test_leaves_seme(graph, train_leaf_fraction=0.75)
    print(f"[2] 节点={vocab['n_nodes']}, 链数(臂)={len(graph['paths'])}, "
          f"train_leaves={len(train_leaves)}, test_leaves={len(test_leaves)}")
    print(f"    vocab_size={vocab['vocab_size']}, root={graph['root']}")
    print(f"    path 示例: {graph['paths'][0]}")

    # ── 构造数据集（复用原仓库 data.py）──
    max_seq_len = max(len(p) for p in graph["paths"]) + 3  # +3 for [PATH][leaf][SEP] prefix
    max_seq_len = max(max_seq_len, 8)  # 至少 8
    dataset = build_dataset(graph, vocab, train_leaves, test_leaves, max_seq_len, vocab["pad_id"])
    print(f"[3] 数据集: edge={dataset['edge'].shape}, train_path={dataset['train_path'].shape}, "
          f"test_path={dataset['test_path'].shape}")

    # ── DeepWalk 预对齐 ──
    d_model = 64  # 小尺寸本地跑通
    print(f"[4] 运行 DeepWalk (dim={d_model}) ...")
    dw_emb = run_deepwalk_seme(graph, vocab, d_model=d_model, seed=42)
    val = validate_pretrained_embeddings_seme(dw_emb, vocab, graph)
    hop_val = validate_deepwalk_hop(dw_emb, vocab, graph)
    print(f"    嵌入校验 arm_cluster (名义, 义素图期望≈0.201): {val}")
    print(f"    嵌入校验 hop (义素图适用): hop1-far={hop_val['hop1_minus_far']:+.4f} "
          f"pass={hop_val['pass']}")

    # ── 模型配置（缩小版）──
    n_layer = 2
    n_head = 4
    model = PathStarTransformer(
        vocab_size=vocab["vocab_size"],
        d_model=d_model,
        n_head=n_head,
        n_layer=n_layer,
        max_seq_len=max_seq_len,
    ).to(device)
    print(f"[5] 模型参数量: {model.count_params()}")

    # ── 注入预对齐嵌入（α=1.0 完全预对齐）──
    model.emb.weight.data.copy_(torch.from_numpy(dw_emb).to(device))
    # 注入后检查 baseline 指标
    acc_before = eval_hardest_token_acc(model, vocab, graph, test_leaves, device)
    sr_before = compute_stable_rank(model, graph, vocab)["stable_rank"]
    auc_before = eval_embedding_auc(model, vocab, graph, device)
    print(f"[6] 训练前 baseline: acc={acc_before:.3f}, sr={sr_before:.2f}, auc={auc_before:.3f}")

    # ── 训练（复用原仓库 train.py 的 get_lr + AdamW + MixedBatchSampler）──
    # 步数注：path-star 基线需 ~954 步方达 50% hardest acc；义素图更难，
    # 500 步内 acc 全 0 属预期（2026-10-14 诊断），故加长到 3000 步。
    train_steps = 3000
    batch_size = 16
    lr_peak = 3e-4
    eval_every = 50

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr_peak, betas=(0.9, 0.95), eps=1e-8)
    sampler = MixedBatchSampler(dataset, batch_size, edge_path_mix_ratio=1.0, seed=42)
    log = []
    best_acc = -1.0

    from tqdm.auto import tqdm
    pbar = tqdm(range(1, train_steps + 1), desc="train")
    for step in pbar:
        model.train()
        batch = sampler.sample_batch().to(device)
        logits = model(batch[:, :-1])
        targets = batch[:, 1:].contiguous()
        loss = F.cross_entropy(
            logits.reshape(-1, logits.size(-1)),
            targets.reshape(-1),
            ignore_index=vocab["pad_id"],
        )
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        lr = get_lr(step, warmup_steps=500, total_steps=train_steps, peak_lr=lr_peak)
        for g in optimizer.param_groups:
            g["lr"] = lr
        optimizer.step()

        if step % eval_every == 0 or step == train_steps:
            model.eval()
            with torch.no_grad():
                acc = eval_hardest_token_acc(model, vocab, graph, test_leaves, device)
            sr = compute_stable_rank(model, graph, vocab)["stable_rank"]
            entry = {"step": step, "loss": float(loss.item()),
                     "hardest_token_acc": float(acc), "stable_rank": sr}
            log.append(entry)
            pbar.set_postfix(loss=f"{loss.item():.4f}", acc=f"{acc:.4f}", sr=f"{sr:.2f}")
            if acc > best_acc:
                best_acc = acc

    # ── 训练后指标 ──
    acc_after = eval_hardest_token_acc(model, vocab, graph, test_leaves, device)
    sr_after = compute_stable_rank(model, graph, vocab)["stable_rank"]
    auc_after = eval_embedding_auc(model, vocab, graph, device)
    align_after = eval_alignment_delta(model, vocab, graph)
    tc = compute_translation_consistency(model, vocab, graph)

    print(f"\n[7] 训练后: acc={acc_after:.3f}, sr={sr_after:.2f}, auc={auc_after:.3f}, "
          f"align={align_after:.3f}")
    print("    注: auc/alignment_delta 为单臂 path-star 度量，义素图上不按字面判读")
    print(f"    translation_consistency: mean={tc['mean_global_consistency']:.3f}, "
          f"inter_arm_std={tc['inter_arm_angle_std']:.1f}°")
    print(f"    best_acc={best_acc:.3f}")

    # ── 对比：不注入预对齐（随机初始化训练）──
    print(f"\n[8] 对比：随机初始化训练（无预对齐）")
    model_rand = PathStarTransformer(
        vocab_size=vocab["vocab_size"],
        d_model=d_model,
        n_head=n_head,
        n_layer=n_layer,
        max_seq_len=max_seq_len,
    ).to(device)
    # 不注入预对齐嵌入，用默认随机初始化
    acc_rand_before = eval_hardest_token_acc(model_rand, vocab, graph, test_leaves, device)
    sr_rand_before = compute_stable_rank(model_rand, graph, vocab)["stable_rank"]
    print(f"    随机 baseline: acc={acc_rand_before:.3f}, sr={sr_rand_before:.2f}")

    optimizer_rand = torch.optim.AdamW(model_rand.parameters(), lr=lr_peak, betas=(0.9, 0.95), eps=1e-8)
    sampler_rand = MixedBatchSampler(dataset, batch_size, edge_path_mix_ratio=1.0, seed=43)
    log_rand = []

    pbar2 = tqdm(range(1, train_steps + 1), desc="rand-train")
    for step in pbar2:
        model_rand.train()
        batch = sampler_rand.sample_batch().to(device)
        logits = model_rand(batch[:, :-1])
        targets = batch[:, 1:].contiguous()
        loss = F.cross_entropy(
            logits.reshape(-1, logits.size(-1)),
            targets.reshape(-1),
            ignore_index=vocab["pad_id"],
        )
        optimizer_rand.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model_rand.parameters(), 1.0)
        lr = get_lr(step, warmup_steps=500, total_steps=train_steps, peak_lr=lr_peak)
        for g in optimizer_rand.param_groups:
            g["lr"] = lr
        optimizer_rand.step()

        if step % eval_every == 0 or step == train_steps:
            model_rand.eval()
            with torch.no_grad():
                acc = eval_hardest_token_acc(model_rand, vocab, graph, test_leaves, device)
            sr = compute_stable_rank(model_rand, graph, vocab)["stable_rank"]
            log_rand.append({"step": step, "loss": float(loss.item()),
                             "hardest_token_acc": float(acc), "stable_rank": sr})
            pbar2.set_postfix(loss=f"{loss.item():.4f}", acc=f"{acc:.4f}", sr=f"{sr:.2f}")

    acc_rand_after = eval_hardest_token_acc(model_rand, vocab, graph, test_leaves, device)
    sr_rand_after = compute_stable_rank(model_rand, graph, vocab)["stable_rank"]
    print(f"    随机训练后: acc={acc_rand_after:.3f}, sr={sr_rand_after:.2f}")

    # ── 摘要 ──
    print("\n" + "=" * 60)
    print("冒烟测试结果摘要")
    print("=" * 60)
    print(f"  预对齐模型:  acc {acc_before:.3f}→{acc_after:.3f}  "
          f"sr {sr_before:.1f}→{sr_after:.1f}  auc={auc_after:.3f}")
    print(f"  随机模型:    acc {acc_rand_before:.3f}→{acc_rand_after:.3f}  "
          f"sr {sr_rand_before:.1f}→{sr_rand_after:.1f}")
    print(f"  DeepWalk 校验: arm_cluster_acc={val['arm_cluster_acc']:.3f}（名义，期望≈0.201）")
    print(f"  DeepWalk 校验: hop1-far={hop_val['hop1_minus_far']:+.4f} pass={hop_val['pass']}（义素图适用）")

    # ── 存图 + JSON ──
    save_dir = os.path.join(os.path.dirname(__file__), "..", "results")
    os.makedirs(save_dir, exist_ok=True)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    steps_pre = [e["step"] for e in log]
    accs_pre = [e["hardest_token_acc"] for e in log]
    srs_pre = [e["stable_rank"] for e in log]
    steps_rand = [e["step"] for e in log_rand]
    accs_rand = [e["hardest_token_acc"] for e in log_rand]
    srs_rand = [e["stable_rank"] for e in log_rand]
    axes[0].plot(steps_pre, accs_pre, "-o", label="pre-aligned")
    axes[0].plot(steps_rand, accs_rand, "-s", label="random")
    axes[0].set_xlabel("step"); axes[0].set_ylabel("hardest_token_acc"); axes[0].legend()
    axes[1].plot(steps_pre, srs_pre, "-o", label="pre-aligned")
    axes[1].plot(steps_rand, srs_rand, "-s", label="random")
    axes[1].set_xlabel("step"); axes[1].set_ylabel("stable_rank"); axes[1].legend()
    plt.tight_layout()
    fig_path = os.path.join(save_dir, "smoke_test_curves.png")
    plt.savefig(fig_path, dpi=150)
    print(f"\n曲线图已保存: {fig_path}")

    results = {
        "pre_aligned": {
            "acc_before": acc_before, "acc_after": acc_after,
            "sr_before": sr_before, "sr_after": sr_after,
            "auc": auc_after, "alignment_delta": align_after,
        },
        "random": {
            "acc_before": acc_rand_before, "acc_after": acc_rand_after,
            "sr_before": sr_rand_before, "sr_after": sr_rand_after,
        },
        "deepwalk_val": val,
        "deepwalk_hop_val": hop_val,
    }
    json_path = os.path.join(save_dir, "smoke_test_results.json")
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"结果 JSON 已保存: {json_path}")
    print("\n冒烟测试通过！管线可正常工作。")


if __name__ == "__main__":
    smoke_test()
