# -*- coding: utf-8 -*-
"""HybridRAG：正则显式候选与 RAG 语义候选的合并逻辑。"""
from embeddings import cosine
from jev_rules import _hit_negated


def rank_labels(query_vector, label_vectors, labels):
    """返回按相似度降序的 [(score, label)]；同分时按标签 idx 保证可复现。"""
    scored = [(cosine(query_vector, vector), label)
              for vector, label in zip(label_vectors, labels)]
    return sorted(scored, key=lambda item: (-item[0], item[1]['idx']))


def merge_candidates(regex_details, scored, top_k, min_score=None):
    """合并正则集合与 RAG Top-K，返回按 idx 排序的候选详情。

    regex_details: {label_idx: screen_label_detail(...) 的结果}
    scored: rank_labels(...) 的全量排序结果
    正则命中不受 min_score 影响；同一个标签两路命中时只保留一个候选。
    """
    rag = {}
    for rank, (score, label) in enumerate(scored, 1):
        if rank > top_k:
            break
        if min_score is not None and score < min_score:
            continue
        rag[label['idx']] = {'score': score, 'rank': rank}

    regex_hits = {idx for idx, detail in regex_details.items() if detail.get('matched')}
    by_idx = {label['idx']: label for _, label in scored}
    selected = []
    for idx in sorted(regex_hits | set(rag)):
        is_regex = idx in regex_hits
        is_rag = idx in rag
        semantic = rag.get(idx, {})
        selected.append({
            'label': by_idx[idx],
            'source': 'both' if is_regex and is_rag else ('regex' if is_regex else 'rag'),
            'regex': regex_details.get(idx, {}),
            'rag_score': semantic.get('score'),
            'rag_rank': semantic.get('rank'),
        })
    return selected



def strong_regex_detail(text, label, scope='window'):
    """标签名精确出现且附近无否定时直接打标；否定命中保留复核。

    只匹配标签名本身，不把 hits 中的近义词直接打成标签。
    direct=True  -> 至少一个精确命中位置未触发排除词；
    matched=True -> 原文出现过标签名（包括需要 Jev 复核的否定命中）。
    """
    name = label['label']
    result = {'matched': False, 'direct': False, 'status': '', 'hit': name,
              'position': None, 'evidence': '', 'scope': scope}
    if not name:
        return result
    positions = []
    start = text.find(name)
    while start >= 0:
        positions.append(start)
        start = text.find(name, start + 1)
    if not positions:
        return result
    result['matched'] = True
    near = label.get('near') or 15
    excls = label.get('excls') or []
    first = positions[0]
    result.update({'status': 'review', 'position': first,
                   'evidence': text[max(0, first - near):first + len(name) + near]})
    if scope == 'off' or not excls:
        clean = first
    elif scope == 'full':
        clean = None if any(ex in text for ex in excls) else first
    else:
        clean = next((pos for pos in positions
                      if not _hit_negated(text, pos, name, excls, near)), None)
    if clean is not None:
        result.update({'direct': True, 'status': 'direct', 'position': clean,
                       'evidence': text[max(0, clean - near):clean + len(name) + near]})
    return result
