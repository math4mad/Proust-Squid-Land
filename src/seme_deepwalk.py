"""
义素超立方体的 DeepWalk 预对齐嵌入。

优先用 gensim（和原仓库 deepwalk.py 一致）；
本地无 gensim 时回退到纯 numpy skip-gram。
"""

import random
from collections import Counter
import numpy as np
import torch


def _build_adj_list(graph):
    """从 graph 构建邻接表 (dict[str, list[str]])。"""
    adj = {}
    for u, v in graph["edges_forward"] + graph["edges_reverse"]:
        adj.setdefault(u, []).append(v)
        adj.setdefault(v, []).append(u)
    return adj


def generate_random_walks_seme(graph, walk_length=10, walks_per_node=40, seed=42):
    """和原仓库 generate_random_walks 一致，在义素超立方体上跑随机游走。"""
    rng = random.Random(seed)
    adj = _build_adj_list(graph)
    all_nodes = list(adj.keys())
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
    return walks


def _stable_sigmoid(x):
    x = np.clip(x, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-x))


def run_deepwalk_seme_numpy(graph, vocab, d_model=256,
                            walk_length=10, walks_per_node=40,
                            window=5, epochs=3, seed=42):
    """纯 numpy skip-gram DeepWalk（无 gensim 时的回退）。"""
    rng = random.Random(seed)
    nprng = np.random.RandomState(seed)
    vocab_size = vocab["vocab_size"]
    token2id = vocab["token2id"]

    walks = generate_random_walks_seme(graph, walk_length, walks_per_node, seed)

    # 窗口共现计数（用 vocab id 直接索引）
    cooccur = Counter()
    for walk in walks:
        walk_ids = [token2id[t] for t in walk]
        for i, center in enumerate(walk_ids):
            lo, hi = max(0, i - window), min(len(walk_ids), i + window + 1)
            for j in range(lo, hi):
                if j != i:
                    cooccur[(center, walk_ids[j])] += 1

    # skip-gram 负采样：emb/out 用 vocab_size 维，直接用 vocab id 索引
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

    # 归一化节点嵌入（special tokens 保持为 0，和原仓库一致）
    node_ids = [vocab["token2id"][t] for t in graph["node_tokens"]]
    node_ids.append(vocab["token2id"][graph["root"]])
    node_ids = list(set(node_ids))
    norms = np.linalg.norm(emb[node_ids], axis=1, keepdims=True)
    emb[node_ids] = emb[node_ids] / (norms + 1e-12)

    # special tokens = 0（和原仓库 run_deepwalk 一致）
    for t in ["[PAD]", "[BOS]", "[EOS]", "[SEP]", "[EDGE]", "[PATH]"]:
        emb[vocab["token2id"][t]] = 0.0

    return emb


def run_deepwalk_seme(graph, vocab, d_model=256,
                      walk_length=10, walks_per_node=40,
                      window=5, seed=42, min_count=1, workers=4):
    """
    用 gensim 跑 DeepWalk（和原仓库 run_deepwalk 一致）。
    无 gensim 时回退到 numpy 版。
    """
    try:
        from gensim.models import Word2Vec
    except ImportError:
        print("[DeepWalk] gensim 不可用，回退到 numpy skip-gram")
        return run_deepwalk_seme_numpy(graph, vocab, d_model,
                                        walk_length=walk_length,
                                        walks_per_node=walks_per_node,
                                        window=window, epochs=3, seed=seed)

    walks = generate_random_walks_seme(graph, walk_length, walks_per_node, seed)
    model_w2v = Word2Vec(
        sentences=walks,
        vector_size=d_model,
        window=window,
        min_count=min_count,
        sg=1,
        workers=workers,
        seed=seed,
        epochs=1,
    )

    emb_matrix = np.zeros((vocab["vocab_size"], d_model), dtype=np.float32)
    for token, idx in vocab["token2id"].items():
        if token in model_w2v.wv:
            emb_matrix[idx] = model_w2v.wv[token]
    return emb_matrix


def validate_pretrained_embeddings_seme(emb_matrix, vocab, graph):
    """对齐原仓库 validate_pretrained_embeddings：arm_cluster_acc。"""
    n_arms = len(graph["paths"])
    node_ids, arm_labels = [], []
    for arm_idx, path in enumerate(graph["paths"]):
        for tok in path:  # 义素链没有 root 跳过，全部节点都参与
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
    return {
        "arm_cluster_acc": acc,
        "threshold": 0.7,
        "pass": acc >= 0.7,
    }
