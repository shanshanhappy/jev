# -*- coding: utf-8 -*-
"""
HybridRAG V2 方案 - 增强版标签体系
强正则直接打标 + RAG 语义候选 + Jev 复核
"""
import os
import json
from typing import List, Dict, Any, Set, Tuple
from .label_loader import load_enhanced_labels, get_all_expressions
from .strong_regex import strong_regex_match, filter_candidates
from .rag_retrieval import RAGRetriever


class HybridRAGScheme:
    """
    HybridRAG V2 方案
    
    工作流程：
    1. 强正则：标签名精确出现且无否定 → 直接打标（0 Jev token）
    2. 强正则：标签名出现但有否定 → Jev 复核
    3. RAG：语义相似度检索 Top-K 候选 → Jev 复核
    4. 去重：直接命中的标签不重复送 Jev
    5. 合并：最终标签 = 直接命中 ∪ Jev通过
    """
    
    def __init__(
        self,
        labels_path: str,
        embedding_service,
        rag_cache_path: Optional[str] = None,
    ):
        """
        初始化 HybridRAG 方案
        
        Args:
            labels_path: 增强版标签文件路径（CSV 或 JSON）
            embedding_service: Embedding 服务实例
            rag_cache_path: RAG 向量缓存路径（可选）
        """
        self.labels = load_enhanced_labels(labels_path)
        self.rag_retriever = RAGRetriever(embedding_service, rag_cache_path)
        
        # 索引标签向量
        print(f"加载 {len(self.labels)} 个增强版标签...")
        self.rag_retriever.index_labels(
            self.labels,
            include_definition=True,
            include_examples=True,
        )
    
    def process_post(
        self,
        text: str,
        post_id: str,
        top_k: int = 50,
        candidate_scan_k: int = 200,
        min_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        处理单个帖子
        
        Args:
            text: 帖子文本
            post_id: 帖子ID
            top_k: RAG Top-K 候选数
            candidate_scan_k: RAG 扫描候选数（用于审计）
            min_score: RAG 最低相似度
            
        Returns:
            处理结果：
            - post_id: 帖子ID
            - direct_hits: 强正则直接命中的标签集合
            - review_hits: 需要Jev复核的标签集合
            - rag_candidates: RAG候选列表 [(label, score), ...]
            - jev_candidates: 最终送Jev的标签集合
            - evidence: 证据信息
        """
        # Step 1: 强正则匹配
        regex_result = strong_regex_match(text, self.labels)
        direct_hits = regex_result['direct_hits']
        review_hits = regex_result['review_hits']
        evidence = regex_result['evidence']
        
        # Step 2: RAG 检索
        rag_candidates = self.rag_retriever.retrieve(
            text,
            self.labels,
            top_k=candidate_scan_k,  # 扫描更多候选用于审计
            min_score=min_score,
        )
        
        # Step 3: 筛选 Top-K（用于实际送 Jev）
        rag_top_k = set(label for label, _ in rag_candidates[:top_k])
        
        # Step 4: 合并候选（去重）
        # 直接命中的不送 Jev
        jev_candidates = (review_hits | rag_top_k) - direct_hits
        
        # 记录 RAG 候选的相似度分数
        rag_scores = {label: score for label, score in rag_candidates}
        
        return {
            'post_id': post_id,
            'direct_hits': direct_hits,
            'review_hits': review_hits,
            'rag_candidates': rag_candidates,
            'rag_top_k': rag_top_k,
            'jev_candidates': jev_candidates,
            'evidence': evidence,
            'rag_scores': rag_scores,
        }
    
    def generate_jev_request(
        self,
        result: Dict[str, Any],
        text: str,
    ) -> List[Dict[str, Any]]:
        """
        生成 Jev 请求
        
        Args:
            result: process_post 的结果
            text: 帖子文本
            
        Returns:
            Jev 请求列表
        """
        post_id = result['post_id']
        jev_candidates = result['jev_candidates']
        
        # 为每个候选标签生成问题
        questions = {}
        for label_name in jev_candidates:
            label = self._find_label(label_name)
            if label:
                question_id = label['label_id']
                questions[question_id] = self._build_a2_question(label)
        
        # 构建请求
        request = {
            'sample': post_id,
            'mode': 'hybrid_rag_v2',
            'stage': 'leaf',
            'scheme': 'HybridRAGV2',
            'state': {
                'content': text,
            },
            'questions': questions,
            'meta': {
                'direct_hits': list(result['direct_hits']),
                'review_hits': list(result['review_hits']),
                'rag_candidates': [
                    {'label': label, 'score': score}
                    for label, score in result['rag_candidates'][:50]  # 只记录 Top-50
                ],
            },
        }
        
        return [request]
    
    def merge_results(
        self,
        direct_hits: Set[str],
        jev_responses: Dict[str, float],
        tau: float = 0.6,
    ) -> Set[str]:
        """
        合并强正则直接命中和 Jev 响应
        
        Args:
            direct_hits: 强正则直接命中的标签
            jev_responses: Jev 响应 {question_id: probability}
            tau: 阈值
            
        Returns:
            最终标签集合
        """
        # 直接命中的标签直接加入
        final_labels = set(direct_hits)
        
        # Jev 响应中概率 >= tau 的标签加入
        for question_id, prob in jev_responses.items():
            if prob >= tau:
                label = self._find_label_by_id(question_id)
                if label:
                    final_labels.add(label['label'])
        
        return final_labels
    
    def evaluate_candidate_recall(
        self,
        results: List[Dict[str, Any]],
        gold_labels: Dict[str, Set[str]],
    ) -> Dict[str, Any]:
        """
        评估候选召回率
        
        Args:
            results: 多个帖子的处理结果列表
            gold_labels: 标准答案 {post_id: set(labels)}
            
        Returns:
            评估指标：
            - direct_precision: 强正则直接命中率
            - direct_recall: 强正则直接召回率
            - regex_recall: 强正则总召回率（直接+复核）
            - rag_recall: RAG 召回率
            - combined_recall: 合并召回率
        """
        # 统计指标
        total_gold = 0
        direct_hits_gold = 0
        review_hits_gold = 0
        rag_hits_gold = 0
        combined_hits_gold = 0
        
        for result in results:
            post_id = result['post_id']
            if post_id not in gold_labels:
                continue
            
            gold = gold_labels[post_id]
            total_gold += len(gold)
            
            direct_hits_gold += len(gold & result['direct_hits'])
            review_hits_gold += len(gold & result['review_hits'])
            rag_hits_gold += len(gold & set(label for label, _ in result['rag_candidates'][:50]))
            
            # 合并候选（直接+复核+RAG）
            combined = result['direct_hits'] | result['review_hits'] | set(label for label, _ in result['rag_candidates'][:50])
            combined_hits_gold += len(gold & combined)
        
        return {
            'total_gold': total_gold,
            'direct_hits_gold': direct_hits_gold,
            'review_hits_gold': review_hits_gold,
            'rag_hits_gold': rag_hits_gold,
            'combined_hits_gold': combined_hits_gold,
            'direct_precision': direct_hits_gold / sum(len(r['direct_hits']) for r in results) if results else 0,
            'direct_recall': direct_hits_gold / total_gold if total_gold else 0,
            'regex_recall': (direct_hits_gold + review_hits_gold) / total_gold if total_gold else 0,
            'rag_recall': rag_hits_gold / total_gold if total_gold else 0,
            'combined_recall': combined_hits_gold / total_gold if total_gold else 0,
        }
    
    def _find_label(self, label_name: str) -> Optional[Dict[str, Any]]:
        """根据标签名查找标签"""
        for label in self.labels:
            if label['label'] == label_name:
                return label
        return None
    
    def _find_label_by_id(self, label_id: str) -> Optional[Dict[str, Any]]:
        """根据标签ID查找标签"""
        for label in self.labels:
            if label['label_id'] == label_id:
                return label
        return None
    
    def _build_a2_question(self, label: Dict[str, Any]) -> Dict[str, Any]:
        """构建 A2 问题（带词库提示）"""
        all_expressions = get_all_expressions(label)
        expr_str = '、'.join(all_expressions[:5]) if all_expressions else ''
        
        instruction = f"原文是否明确表达「{label['label']}」？"
        if expr_str:
            instruction += f"常见说法如{expr_str}，同义改写也算。"
        instruction += "只有原文有对应的明确表述才算。"
        
        return {
            'type': 'noul',
            'instructions': instruction,
        }


if __name__ == '__main__':
    # 测试
    from unittest.mock import Mock
    
    print("HybridRAG V2 方案测试")
    print("=" * 60)
    
    # 创建 mock embedding 服务
    mock_service = Mock()
    mock_service.embed = Mock(return_value=[[0.1, 0.2, 0.3]])
    
    print("\n提示：需要提供真实的增强版标签文件才能运行完整测试")
    print("用法示例：")
    print("  scheme = HybridRAGScheme('data/标签知识增强_生成模板_638.csv', embedding_service)")
    print("  result = scheme.process_post('这款面霜很保湿', 'post_001')")
    print("  print(result['direct_hits'])")
