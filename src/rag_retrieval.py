# -*- coding: utf-8 -*-
"""
RAG 检索逻辑 - 增强版标签体系 (修复同名标签覆盖问题)
核心修改：使用 label_id 作为向量缓存和检索结果的主键，避免同名标签互相覆盖。
"""
import json
import os
from typing import List, Dict, Any, Tuple, Optional
from .label_loader import get_all_expressions

class RAGRetriever:
    def __init__(self, embedding_service, cache_path: Optional[str] = None):
        self.embedding_service = embedding_service
        self.cache_path = cache_path
        # ✅ 修复：key 改为 label_id，彻底解决同名标签覆盖问题
        self.label_vectors = {} 
        self._load_cache()

    def _load_cache(self):
        if self.cache_path and os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, 'r', encoding='utf-8') as f:
                    self.label_vectors = json.load(f)
            except Exception:
                self.label_vectors = {}

    def _save_cache(self):
        if self.cache_path:
            try:
                with open(self.cache_path, 'w', encoding='utf-8') as f:
                    json.dump(self.label_vectors, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def build_label_text(self, label: Dict[str, Any]) -> str:
        """构建标签的 embedding 文本：标签名 + 定义 + 常见表达 + 正例"""
        parts = [f"标签：{label['label']}"]
        if label.get('definition'):
            parts.append(f"定义：{label['definition']}")
        all_exprs = get_all_expressions(label)
        if all_exprs:
            parts.append(f"常见表达：{'、'.join(all_exprs[:10])}")
        examples = []
        if label.get('explicit_example'):
            examples.append(label['explicit_example'])
        examples.extend([ex for ex in label.get('semantic_examples', []) if ex])
        if examples:
            parts.append(f"正例：{'；'.join(examples[:4])}")
        return '。'.join(parts)

    def index_labels(self, labels: List[Dict[str, Any]], force_recompute: bool = False):
        """索引标签向量，以 label_id 为唯一键"""
        new_labels = [l for l in labels if force_recompute or l['label_id'] not in self.label_vectors]
        if not new_labels: return
        texts = [self.build_label_text(l) for l in new_labels]
        vectors = self.embedding_service.embed(texts)
        for l, v in zip(new_labels, vectors):
            self.label_vectors[l['label_id']] = v  # ✅ 用 label_id 存储
        self._save_cache()

    def retrieve(self, text: str, labels: List[Dict[str, Any]], top_k: int = 50, min_score: Optional[float] = None) -> List[Tuple[str, float]]:
        """检索 Top-K 候选，返回 [(label_id, score), ...]"""
        if not self.label_vectors:
            self.index_labels(labels)
        post_vec = self.embedding_service.embed([text])[0]
        scores = []
        for l in labels:
            lid = l['label_id']
            if lid not in self.label_vectors: continue
            score = self._cosine(post_vec, self.label_vectors[lid])
            if min_score is None or score >= min_score:
                scores.append((lid, score))  # ✅ 返回 label_id
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    def _cosine(self, v1, v2) -> float:
        dot = sum(a*b for a,b in zip(v1,v2))
        n1 = sum(a*a for a in v1)**0.5
        n2 = sum(b*b for b in v2)**0.5
        return dot/(n1*n2) if n1 and n2 else 0.0
