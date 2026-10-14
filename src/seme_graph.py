"""
义素超立方体适配层 — 把义素图"伪装"成原仓库 graph.py 的接口。

核心思路：
  - 原仓库的 graph = {paths, edges_forward, edges_reverse, root}
  - 义素超立方体的"臂" = 固定 n_semes-1 维，沿第 dim 维遍历 n_values 个值
  - 每条义素链的末尾节点 = 叶（unseen）
  - vocab 对齐原仓库 build_vocab() 的 special tokens

这样原仓库的 model / metrics / data / train 全部可以直接复用，无需修改。
"""

from itertools import product
from typing import List, Tuple, Dict, Set


# ── 原仓库的 special tokens（必须完全一致）──
SPECIAL_TOKENS = ["[PAD]", "[BOS]", "[EOS]", "[SEP]", "[EDGE]", "[PATH]"]


def build_seme_hypercube(n_semes: int = 4, n_values: int = 4) -> dict:
    """
    构造义素超立方体图，输出对齐原仓库 graph.py 的接口。

    返回 graph dict:
      - paths: list of list[str]  每条义素链 = [root, hop1, ..., leaf]
      - edges_forward: list of (str, str)
      - edges_reverse: list of (str, str)
      - root: str  虚拟根节点 token "v_root"

    设计：
      - 节点 = 义素组合 (n_values^n_semes 个)
      - "臂"(义素链) = 固定 n_semes-1 维，沿第 dim 维遍历 n_values 个值
      - 每条链前加一个共享虚拟根 "v_root"（和原 path-star 完全同构）
        这样 path = [v_root, chain_0, chain_1, ..., chain_{L-1}]
        leaf = path[-1]，_build_graph_node_index 的 path[1:] 逻辑正确
      - 每条链的末尾节点 = 叶（unseen）
    """
    # 虚拟根（和原 path-star 一致）
    root = "v_root"

    # 所有组合
    combos = list(product(range(n_values), repeat=n_semes))

    def _token(combo):
        return "_".join(f"s{i}={combo[i]}" for i in range(n_semes))

    node_tokens = [_token(c) for c in combos]

    # 构造义素链 paths = [root, chain_0, ..., chain_{L-1}]
    # 沿 dim 维遍历 n_values 个值，其余维度固定
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

    # 构造边：root→chain_0 + 每条链上相邻节点对
    edges_forward = []
    for path_tokens in paths:
        for k in range(len(path_tokens) - 1):
            edges_forward.append((path_tokens[k], path_tokens[k + 1]))
    edges_forward = list(dict.fromkeys(edges_forward))  # 去重
    edges_reverse = [(b, a) for (a, b) in edges_forward]

    graph = {
        "paths": paths,
        "edges_forward": edges_forward,
        "edges_reverse": edges_reverse,
        "root": root,
        "n_semes": n_semes,
        "n_values": n_values,
        "n_nodes": len(node_tokens),
        "node_tokens": node_tokens,
    }
    return graph


def build_vocab_seme(n_semes: int = 4, n_values: int = 4, graph: dict = None) -> dict:
    """
    构造 vocab，对齐原仓库 build_vocab() 的接口。

    special_tokens = [PAD, BOS, EOS, SEP, EDGE, PATH]
    node_tokens = 全部义素组合
    """
    if graph is None:
        graph = build_seme_hypercube(n_semes, n_values)

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
        "n_semes": n_semes,
        "n_values": n_values,
        "n_nodes": len(node_tokens),
    }


def build_train_test_leaves_seme(graph: dict, train_leaf_fraction: float = 0.75,
                                 seed: int = 42) -> Tuple[Set[str], Set[str]]:
    """
    对齐原仓库 build_train_test_leaves()：按顺序切分叶节点。
    """
    all_leaves = [path[-1] for path in graph["paths"]]
    # 去重但保持顺序
    seen = set()
    unique_leaves = []
    for l in all_leaves:
        if l not in seen:
            seen.add(l)
            unique_leaves.append(l)
    n_train = int(len(unique_leaves) * train_leaf_fraction)
    return set(unique_leaves[:n_train]), set(unique_leaves[n_train:])
