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
    print(f"    嵌入校验: {val}")

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
    train_steps = 500
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
    print(f"  DeepWalk 校验: arm_cluster_acc={val['arm_cluster_acc']:.3f}")

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
    }
    json_path = os.path.join(save_dir, "smoke_test_results.json")
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"结果 JSON 已保存: {json_path}")
    print("\n冒烟测试通过！管线可正常工作。")


if __name__ == "__main__":
    smoke_test()
