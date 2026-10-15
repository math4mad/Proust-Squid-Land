# ============================================================
# Kaggle Notebook: 预对齐程度 → 几何速度 实验
# 复用原仓库 llm-geometry-analysis + 义素超立方体适配层
# ============================================================
# 用法:
#   1. Kaggle Notebook 选 GPU (T4 x2)
#   2. Cell 1: !git clone <仓库地址> /kaggle/working/llm-geometry-analysis
#   3. Cell 2: 粘贴本文件内容并运行
#   4. 或上传本文件为 run_experiment.py，在 notebook 中 !python run_experiment.py
# ============================================================

import os
import sys
import math
import json
import random
from itertools import product
from collections import Counter

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from tqdm.auto import tqdm

# ── 路径设置 ──
REPO_PATH = os.environ.get("REPO_PATH", "/kaggle/working/llm-geometry-analysis")
if not os.path.exists(REPO_PATH):
    # Kaggle 舱内自动 clone 公开原仓库（本机回退到本地克隆）
    import subprocess as _sp
    print(f"[setup] {REPO_PATH} 不存在，git clone 原仓库 ...")
    _rc = _sp.call(["git", "clone", "--depth", "1",
                    "https://github.com/rsharma441/llm-geometry-analysis",
                    REPO_PATH])
    if _rc != 0:
        REPO_PATH = "/Users/mac/Documents/GitHub/llm-geometry-analysis"  # 本地回退
        print(f"[setup] clone 失败，回退本地 {REPO_PATH}")
sys.path.insert(0, REPO_PATH)

# 原仓库模块
from src.model import PathStarTransformer
from src.metrics import (
    eval_hardest_token_acc,
    eval_embedding_auc,
    eval_alignment_delta,
    compute_stable_rank,
    compute_translation_consistency,
)
from src.train import get_lr
from src.data import build_dataset, MixedBatchSampler

# ── 义素超立方体适配层（内联，避免包名冲突）──

SPECIAL_TOKENS = ["[PAD]", "[BOS]", "[EOS]", "[SEP]", "[EDGE]", "[PATH]"]


def build_seme_hypercube(n_semes=4, n_values=4):
    """义素超立方体 → 对齐原仓库 graph.py 接口。"""
    root = "v_root"
    combos = list(product(range(n_values), repeat=n_semes))

    def _token(combo):
        return "_".join(f"s{i}={combo[i]}" for i in range(n_semes))

    node_tokens = [_token(c) for c in combos]
    paths = []
    for dim in range(n_semes):
        for fixed in product(range(n_values), repeat=n_semes - 1):
            chain = []
            for v in range(n_values):
                combo = list(fixed)
                combo.insert(dim, v)
                chain.append(tuple(combo))
            path_tokens = [root] + [_token(c) for c in chain]
            paths.append(path_tokens)

    edges_forward = []
    for path_tokens in paths:
        for k in range(len(path_tokens) - 1):
            edges_forward.append((path_tokens[k], path_tokens[k + 1]))
    edges_forward = list(dict.fromkeys(edges_forward))
    edges_reverse = [(b, a) for (a, b) in edges_forward]

    return {
        "paths": paths,
        "edges_forward": edges_forward,
        "edges_reverse": edges_reverse,
        "root": root,
        "n_semes": n_semes,
        "n_values": n_values,
        "n_nodes": len(node_tokens),
        "node_tokens": node_tokens,
    }


def build_vocab_seme(graph):
    """对齐原仓库 build_vocab() 接口。"""
    node_tokens = [graph["root"]] + graph["node_tokens"]
    all_tokens = SPECIAL_TOKENS + node_tokens
    token2id = {t: i for i, t in enumerate(all_tokens)}
    id2token = {i: t for t, i in token2id.items()}
    return {
        "token2id": token2id,
        "id2token": id2token,
        "vocab_size": len(all_tokens),
        "pad_id": token2id["[PAD]"],
        "bos_id": token2id["[BOS]"],
        "eos_id": token2id["[EOS]"],
        "sep_id": token2id["[SEP]"],
        "edge_id": token2id["[EDGE]"],
        "path_id": token2id["[PATH]"],
        "root_id": token2id[graph["root"]],
        "n_semes": graph["n_semes"],
        "n_values": graph["n_values"],
        "n_nodes": graph["n_nodes"],
    }


def build_train_test_leaves_seme(graph, train_leaf_fraction=0.75):
    """对齐原仓库 build_train_test_leaves()。"""
    all_leaves = [path[-1] for path in graph["paths"]]
    seen, unique_leaves = set(), []
    for l in all_leaves:
        if l not in seen:
            seen.add(l)
            unique_leaves.append(l)
    n_train = int(len(unique_leaves) * train_leaf_fraction)
    return set(unique_leaves[:n_train]), set(unique_leaves[n_train:])


# ── DeepWalk ──

def _build_adj_list(graph):
    adj = {}
    for u, v in graph["edges_forward"] + graph["edges_reverse"]:
        adj.setdefault(u, []).append(v)
        adj.setdefault(v, []).append(u)
    return adj


def _stable_sigmoid(x):
    x = np.clip(x, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-x))


def run_deepwalk_seme(graph, vocab, d_model, walk_length=10,
                      walks_per_node=40, window=5, seed=42):
    """DeepWalk（优先 gensim，回退 numpy）。"""
    try:
        from gensim.models import Word2Vec
        use_gensim = True
    except ImportError:
        print("[DeepWalk] gensim 不可用，回退到 numpy skip-gram")
        use_gensim = False

    adj = _build_adj_list(graph)
    all_nodes = list(adj.keys())
    rng = random.Random(seed)

    # 随机游走
    walks = []
    for _ in range(walks_per_node):
        rng.shuffle(all_nodes)
        for start in all_nodes:
            walk = [start]
            for _ in range(walk_length - 1):
                cur = walk[-1]
                nbrs = adj.get(cur, [])
                if not nbrs:
                    break
                walk.append(rng.choice(nbrs))
            walks.append(walk)

    if use_gensim:
        model_w2v = Word2Vec(
            sentences=walks, vector_size=d_model, window=window,
            min_count=1, sg=1, workers=4, seed=seed, epochs=1)
        emb = np.zeros((vocab["vocab_size"], d_model), dtype=np.float32)
        for token, idx in vocab["token2id"].items():
            if token in model_w2v.wv:
                emb[idx] = model_w2v.wv[token]
        return emb
    else:
        return _deepwalk_numpy(walks, vocab, d_model, window=window, seed=seed)


def _deepwalk_numpy(walks, vocab, d_model, window=5, epochs=3, seed=42):
    """numpy skip-gram 回退。"""
    rng = random.Random(seed + 1)
    nprng = np.random.RandomState(seed + 1)
    vocab_size = vocab["vocab_size"]
    token2id = vocab["token2id"]

    cooccur = Counter()
    for walk in walks:
        walk_ids = [token2id[t] for t in walk]
        for i, center in enumerate(walk_ids):
            lo, hi = max(0, i - window), min(len(walk_ids), i + window + 1)
            for j in range(lo, hi):
                if j != i:
                    cooccur[(center, walk_ids[j])] += 1

    emb = nprng.normal(0, 0.1, (vocab_size, d_model)).astype(np.float32)
    out = nprng.normal(0, 0.1, (vocab_size, d_model)).astype(np.float32)
    neg_count = 5
    freq = np.ones(vocab_size) / vocab_size
    lr = 0.025

    for _ in range(epochs):
        items = list(cooccur.items())
        rng.shuffle(items)
        for (center, ctx), cnt in items:
            cnt = min(cnt, 5)
            score = float(emb[center] @ out[ctx])
            sig = _stable_sigmoid(score)
            grad = (sig - 1.0) * cnt
            emb[center] -= lr * grad * out[ctx]
            out[ctx] -= lr * grad * emb[center]
            for _ in range(neg_count):
                neg = nprng.choice(vocab_size, p=freq / freq.sum())
                if neg == ctx:
                    continue
                nscore = float(emb[center] @ out[neg])
                nsig = _stable_sigmoid(nscore)
                ngrad = nsig * cnt
                emb[center] -= lr * ngrad * out[neg]
                out[neg] -= lr * ngrad * emb[center]
            np.clip(emb[center], -10, 10, out=emb[center])
            np.clip(out[ctx], -10, 10, out=out[ctx])

    bad = ~np.isfinite(emb).all(axis=1)
    if bad.any():
        emb[bad] = nprng.normal(0, 0.1, (bad.sum(), d_model)).astype(np.float32)
    return emb


def validate_pretrained_embeddings_seme(emb_matrix, vocab, graph):
    """arm_cluster_acc 校验。"""
    n_arms = len(graph["paths"])
    node_ids, arm_labels = [], []
    for arm_idx, path in enumerate(graph["paths"]):
        for tok in path:
            node_ids.append(vocab["token2id"][tok])
            arm_labels.append(arm_idx)
    E = emb_matrix[node_ids]
    labels = np.array(arm_labels)
    norms = np.linalg.norm(E, axis=1, keepdims=True)
    E_norm = E / np.where(norms > 1e-12, norms, 1.0)
    centroids = np.stack([E_norm[labels == i].mean(axis=0) for i in range(n_arms)])
    c_norms = np.linalg.norm(centroids, axis=1, keepdims=True)
    centroids = centroids / np.where(c_norms > 1e-12, c_norms, 1.0)
    sims = E_norm @ centroids.T
    preds = sims.argmax(axis=1)
    acc = float((preds == labels).mean())
    return {"arm_cluster_acc": acc, "pass": acc >= 0.7}


# ── 预对齐条件构造 ──

def gen_conditions():
    """7 α + 6 k + 2 对照 = 15 条件。"""
    conds = {}
    for i, alpha in enumerate(np.linspace(0, 1, 7)):
        conds[f"alpha_{i}"] = {"mode": "alpha", "alpha": float(alpha)}
    for k in [2, 4, 8, 16, 32, 64]:
        conds[f"k_{k}"] = {"mode": "spectral", "k": k}
    conds["scale_ctrl"] = {"mode": "scale", "factor": 1.0}
    conds["perm_ctrl"] = {"mode": "perm", "perm_seed": 0}
    return conds


def _build_graph_node_index_local(graph, vocab):
    """和原仓库 _build_graph_node_index 一致。"""
    node_tokens = [graph["root"]]
    for path in graph["paths"]:
        for tok in path[1:]:
            node_tokens.append(tok)
    node_tokens = list(dict.fromkeys(node_tokens))
    node_to_idx = {t: i for i, t in enumerate(node_tokens)}
    vocab_ids = [vocab["token2id"][t] for t in node_tokens]
    return node_tokens, node_to_idx, vocab_ids


def make_init(graph, vocab, d_model, cond, device, deepwalk_emb):
    """按条件生成初始嵌入矩阵 (vocab_size, d_model)。"""
    emb = torch.zeros((vocab["vocab_size"], d_model), dtype=torch.float32)
    mode = cond["mode"]
    rng = np.random.default_rng(0)
    n_nodes = vocab["n_nodes"] + 1  # 含 v_root

    if mode == "alpha":
        a = cond["alpha"]
        rnd = torch.from_numpy(rng.standard_normal((vocab["vocab_size"], d_model)).astype(np.float32)) * 0.02
        emb = a * torch.from_numpy(deepwalk_emb).float() + (1 - a) * rnd
    elif mode == "spectral":
        import scipy.sparse as sp
        import scipy.sparse.linalg as spla
        node_tokens, node_to_idx, vocab_ids = _build_graph_node_index_local(graph, vocab)
        n = len(node_to_idx)
        rows, cols = [], []
        for u, v in graph["edges_forward"] + graph["edges_reverse"]:
            rows.append(node_to_idx[u])
            cols.append(node_to_idx[v])
        A = sp.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n))
        D = sp.diags(np.asarray(A.sum(axis=1)).ravel())
        L = D - A
        k_eff = min(cond["k"], n - 2)
        _, V = spla.eigsh(L.astype(np.float64), k=k_eff, which="SM")
        proj = V @ V.T
        dw_np = deepwalk_emb
        dw_aligned = np.zeros((n, d_model), dtype=np.float32)
        for i, vid in enumerate(vocab_ids):
            dw_aligned[i] = dw_np[vid]
        proj_emb = (proj @ dw_aligned).astype(np.float32)
        emb_np = np.zeros((vocab["vocab_size"], d_model), dtype=np.float32)
        for i, vid in enumerate(vocab_ids):
            emb_np[vid] = proj_emb[i]
        emb = torch.from_numpy(emb_np)
    elif mode == "scale":
        emb = torch.from_numpy(deepwalk_emb.copy()).float()
    elif mode == "perm":
        # 只打乱节点 token 的行（跳过 special tokens）
        offset = len(SPECIAL_TOKENS)
        perm = np.random.default_rng(cond["perm_seed"]).permutation(n_nodes)
        emb_np = deepwalk_emb.copy()
        node_region = emb_np[offset:offset + n_nodes].copy()
        emb_np[offset:offset + n_nodes] = node_region[perm]
        emb = torch.from_numpy(emb_np)
    return emb.to(device)


# ── 训练循环 ──

def train_and_track(model, graph, vocab, dataset, test_leaves, device,
                    cfg_train, eval_every=500):
    """训练并返回 log + 最终指标。"""
    optimizer = torch.optim.AdamW(
        [{"params": model.emb.parameters(), "weight_decay": 0.0},
         {"params": [p for n, p in model.named_parameters() if "emb" not in n],
          "weight_decay": 0.01}],
        lr=cfg_train["learning_rate"], betas=(0.9, 0.95), eps=1e-8,
    )
    sampler = MixedBatchSampler(dataset, cfg_train["batch_size"],
                                 edge_path_mix_ratio=1.0, seed=cfg_train["seed"])
    log = []
    best_acc = -1.0

    pbar = tqdm(range(1, cfg_train["train_steps"] + 1), dynamic_ncols=True)
    for step in pbar:
        model.train()
        batch = sampler.sample_batch().to(device)
        logits = model(batch[:, :-1])
        targets = batch[:, 1:].contiguous()
        loss = F.cross_entropy(
            logits.reshape(-1, logits.size(-1)),
            targets.reshape(-1),
            ignore_index=vocab["pad_id"])
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        lr = get_lr(step, warmup_steps=500,
                    total_steps=cfg_train["train_steps"],
                    peak_lr=cfg_train["learning_rate"])
        for g in optimizer.param_groups:
            g["lr"] = lr
        optimizer.step()

        if step % eval_every == 0 or step == cfg_train["train_steps"]:
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
    return log


def steps_to_acc(log, target_acc):
    for i, entry in enumerate(log):
        if entry["hardest_token_acc"] >= target_acc:
            if i == 0:
                return entry["step"]
            prev, cur = log[i - 1], entry
            frac = (target_acc - prev["hardest_token_acc"]) / max(
                cur["hardest_token_acc"] - prev["hardest_token_acc"], 1e-12)
            return int(prev["step"] + frac * (cur["step"] - prev["step"]))
    return None


# ═══════════════════════════════════════════════════════════
# 主流程
# ============================================================
def main():
    # ──── CONFIG ────
    N_SEMES = 4
    N_VALUES = 4
    D_MODEL = 192
    N_HEAD = 6
    N_LAYERS = 4
    LEARNING_RATE = 3e-4
    BATCH_SIZE = 64
    TRAIN_STEPS = 3000       # Kaggle 上可调大
    EVAL_EVERY = 250
    ACC_TARGET = 0.5         # 收敛阈值（义素图比 path-star 更难）
    GEO_THRESHOLD = 5.0      # stable_rank < 5 → 几何成形
    SEEDS = [0]              # 首飞单种子；冒烟已验证管线（2026-10-14），多种子待首飞结果
    SAVE_DIR = "/kaggle/working/results" if os.path.exists("/kaggle/working") else "./results"
    # ────────────────

    os.makedirs(SAVE_DIR, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"device = {device}")

    # 构造图 + vocab
    graph = build_seme_hypercube(N_SEMES, N_VALUES)
    vocab = build_vocab_seme(graph)
    train_leaves, test_leaves = build_train_test_leaves_seme(graph, train_leaf_fraction=0.75)
    print(f"节点={vocab['n_nodes']}, 链数={len(graph['paths'])}, "
          f"train_leaves={len(train_leaves)}, test_leaves={len(test_leaves)}")
    print(f"vocab_size={vocab['vocab_size']}, path 示例: {graph['paths'][0]}")

    # 数据集
    max_seq_len = max(len(p) for p in graph["paths"]) + 3
    max_seq_len = max(max_seq_len, 8)
    dataset = build_dataset(graph, vocab, train_leaves, test_leaves, max_seq_len, vocab["pad_id"])
    print(f"数据集: edge={dataset['edge'].shape}, train_path={dataset['train_path'].shape}")

    # DeepWalk 预对齐
    print("运行 DeepWalk 预对齐 ...")
    deepwalk_emb = run_deepwalk_seme(graph, vocab, d_model=D_MODEL, seed=42)
    val = validate_pretrained_embeddings_seme(deepwalk_emb, vocab, graph)
    print(f"嵌入校验: {val}")

    cfg_model = dict(vocab_size=vocab["vocab_size"], d_model=D_MODEL,
                     n_head=N_HEAD, n_layer=N_LAYERS, max_seq_len=max_seq_len)
    cfg_train_base = dict(learning_rate=LEARNING_RATE, batch_size=BATCH_SIZE,
                          train_steps=TRAIN_STEPS)

    conds = gen_conditions()
    all_rows = []

    for name, cond in conds.items():
        for seed in SEEDS:
            print(f"\n{'='*50}")
            print(f"条件: {name} = {cond}  seed={seed}")
            print(f"{'='*50}")
            torch.manual_seed(seed)
            np.random.seed(seed)
            random.seed(seed)

            model = PathStarTransformer(**cfg_model).to(device)
            init_emb = make_init(graph, vocab, D_MODEL, cond, device, deepwalk_emb)
            model.emb.weight.data.copy_(init_emb)

            cfg_train = dict(cfg_train_base, seed=seed)
            log = train_and_track(model, graph, vocab, dataset, test_leaves,
                                  device, cfg_train, eval_every=EVAL_EVERY)

            # 最终指标
            # 注：auc / alignment_delta 为单臂 path-star 度量；义素超立方体
            # 节点被 4 条链共享（root 被 256 条共享），其值不按字面判读，
            # 仅作记录。能力判据 = hardest_token_acc / steps_to_acc / stable_rank。
            acc = eval_hardest_token_acc(model, vocab, graph, test_leaves, device)
            align = eval_alignment_delta(model, vocab, graph)
            sr = compute_stable_rank(model, graph, vocab)["stable_rank"]
            auc = eval_embedding_auc(model, vocab, graph, device)
            s2a = steps_to_acc(log, ACC_TARGET)

            geo_form_step = None
            for e in log:
                if e["stable_rank"] < GEO_THRESHOLD:
                    geo_form_step = e["step"]
                    break

            row = {
                "cond_name": name, "cond_mode": cond["mode"],
                "alpha": cond.get("alpha"), "k": cond.get("k"),
                "seed": seed,
                "final_acc": acc, f"steps_to_{ACC_TARGET}": s2a,
                "geo_form_step": geo_form_step,
                "final_stable_rank": sr,
                "final_alignment_delta": align,
                "final_auc": auc,
            }
            all_rows.append(row)
            print(f"  → acc={acc:.3f}  steps={s2a}  geo={geo_form_step}  "
                  f"sr={sr:.2f}  auc={auc:.3f}  align={align:.3f}")

            pd.DataFrame(all_rows).to_csv(f"{SAVE_DIR}/partial_results.csv", index=False)

    # ── 汇总 + 画曲线 ──
    df = pd.DataFrame(all_rows)
    df.to_csv(f"{SAVE_DIR}/final_results.csv", index=False)
    print(f"\n结果已保存到 {SAVE_DIR}/final_results.csv")

    fig, axes = plt.subplots(1, 3, figsize=(18, 5))

    alpha_df = df[df.cond_mode == "alpha"].groupby("alpha")[f"steps_to_{ACC_TARGET}"].mean()
    axes[0].plot(alpha_df.index, alpha_df.values, "-o", color="C0")
    axes[0].set_xlabel("α (预对齐程度)")
    axes[0].set_ylabel("收敛步数")
    axes[0].set_title("收敛步数 vs 预对齐程度 (插值)")

    k_df = df[df.cond_mode == "spectral"].groupby("k")[f"steps_to_{ACC_TARGET}"].mean()
    axes[1].plot(k_df.index, k_df.values, "-o", color="C1")
    axes[1].set_xlabel("k (谱截断)")
    axes[1].set_ylabel("收敛步数")
    axes[1].set_title("收敛步数 vs 谱截断 k")

    geo_means = df.groupby("cond_name")["geo_form_step"].mean()
    axes[2].bar(range(len(geo_means)), geo_means.values, color="C2")
    axes[2].set_xticks(range(len(geo_means)))
    axes[2].set_xticklabels(geo_means.index, rotation=90)
    axes[2].set_ylabel("几何成形步数")
    axes[2].set_title("几何成形步数（各条件）")

    plt.tight_layout()
    plt.savefig(f"{SAVE_DIR}/curves.png", dpi=150)
    plt.show()
    print(f"曲线图已保存到 {SAVE_DIR}/curves.png")
    print("\n实验完成！")


if __name__ == "__main__":
    main()
