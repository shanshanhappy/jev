# -*- coding: utf-8 -*-
"""
增强版标签体系加载器
支持从 CSV 或 JSON 加载增强版标签（23字段格式）
"""
import csv
import json
import os
from typing import List, Dict, Any


def load_enhanced_labels(path: str) -> List[Dict[str, Any]]:
    """
    加载增强版标签体系
    
    Args:
        path: CSV 或 JSON 文件路径
        
    Returns:
        标签列表，每个标签包含：
        - label_id: 标签ID
        - label: 标签名
        - existing_expressions: 现有常见表达（列表）
        - common_expressions: 补充后常见表达（完整列表，列表）
        - definition: 标签定义
        - explicit_example: 明确含标签名的正例
        - semantic_examples: 语义正例列表（最多3个）
        - negative_examples: 反例列表（最多3个）
        - negative_reasons: 反例原因列表（最多3个）
        - confusing_labels: 易混标签列表（最多3个）
        - confusing_boundaries: 易混边界列表（最多3个）
        - needs_review: 是否需人工复核
        - review_reason: 复核原因
    """
    ext = os.path.splitext(path)[1].lower()
    
    if ext == '.json':
        return _load_from_json(path)
    elif ext == '.csv':
        return _load_from_csv(path)
    else:
        raise ValueError(f"不支持的文件格式: {ext}，只支持 .csv 或 .json")


def _load_from_json(path: str) -> List[Dict[str, Any]]:
    """从 JSON 加载"""
    with open(path, 'r', encoding='utf-8') as f:
        raw_labels = json.load(f)
    
    labels = []
    for raw in raw_labels:
        label = {
            'label_id': raw.get('标签ID', ''),
            'label': raw.get('标签名', ''),
            'existing_expressions': _parse_expressions(raw.get('现有常见表达', '')),
            'common_expressions': _parse_expressions(raw.get('补充后常见表达（完整列表）', '')),
            'definition': raw.get('标签定义', ''),
            'explicit_example': raw.get('正例_明确含标签名', ''),
            'semantic_examples': [
                raw.get('正例_语义1', ''),
                raw.get('正例_语义2', ''),
                raw.get('正例_语义3', ''),
            ],
            'negative_examples': [
                raw.get('反例1', ''),
                raw.get('反例2', ''),
                raw.get('反例3', ''),
            ],
            'negative_reasons': [
                raw.get('反例1原因', ''),
                raw.get('反例2原因', ''),
                raw.get('反例3原因', ''),
            ],
            'confusing_labels': [
                raw.get('易混标签1', ''),
                raw.get('易混标签2', ''),
                raw.get('易混标签3', ''),
            ],
            'confusing_boundaries': [
                raw.get('易混边界1', ''),
                raw.get('易混边界2', ''),
                raw.get('易混边界3', ''),
            ],
            'needs_review': raw.get('是否需人工复核', '') == '是',
            'review_reason': raw.get('复核原因', ''),
        }
        labels.append(label)
    
    return labels


def _load_from_csv(path: str) -> List[Dict[str, Any]]:
    """从 CSV 加载"""
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        raw_labels = list(reader)
    
    labels = []
    for raw in raw_labels:
        label = {
            'label_id': raw.get('标签ID', ''),
            'label': raw.get('标签名', ''),
            'existing_expressions': _parse_expressions(raw.get('现有常见表达', '')),
            'common_expressions': _parse_expressions(raw.get('补充后常见表达（完整列表）', '')),
            'definition': raw.get('标签定义', ''),
            'explicit_example': raw.get('正例_明确含标签名', ''),
            'semantic_examples': [
                raw.get('正例_语义1', ''),
                raw.get('正例_语义2', ''),
                raw.get('正例_语义3', ''),
            ],
            'negative_examples': [
                raw.get('反例1', ''),
                raw.get('反例2', ''),
                raw.get('反例3', ''),
            ],
            'negative_reasons': [
                raw.get('反例1原因', ''),
                raw.get('反例2原因', ''),
                raw.get('反例3原因', ''),
            ],
            'confusing_labels': [
                raw.get('易混标签1', ''),
                raw.get('易混标签2', ''),
                raw.get('易混标签3', ''),
            ],
            'confusing_boundaries': [
                raw.get('易混边界1', ''),
                raw.get('易混边界2', ''),
                raw.get('易混边界3', ''),
            ],
            'needs_review': raw.get('是否需人工复核', '') == '是',
            'review_reason': raw.get('复核原因', ''),
        }
        labels.append(label)
    
    return labels


def _parse_expressions(text: str) -> List[str]:
    """
    解析常见表达字符串为列表
    支持顿号（、）分隔的格式
    """
    if not text or not text.strip():
        return []
    
    # 按顿号分隔
    parts = text.split('、')
    # 去除空白并过滤空字符串
    return [p.strip() for p in parts if p.strip()]


def get_all_expressions(label: Dict[str, Any]) -> List[str]:
    """
    获取标签的所有常见表达（现有 + 补充后）
    自动去重，优先使用补充后的版本
    """
    all_exprs = []
    
    # 先加补充后的（如果存在）
    if label.get('common_expressions'):
        all_exprs.extend(label['common_expressions'])
    
    # 再加现有的（去重）
    if label.get('existing_expressions'):
        for expr in label['existing_expressions']:
            if expr not in all_exprs:
                all_exprs.append(expr)
    
    return all_exprs


if __name__ == '__main__':
    # 测试加载
    import sys
    if len(sys.argv) < 2:
        print("用法: python label_loader.py <path_to_csv_or_json>")
        sys.exit(1)
    
    path = sys.argv[1]
    labels = load_enhanced_labels(path)
    print(f"成功加载 {len(labels)} 个标签")
    
    if labels:
        print(f"\n第一个标签示例:")
        first = labels[0]
        print(f"  标签ID: {first['label_id']}")
        print(f"  标签名: {first['label']}")
        print(f"  现有常见表达: {first['existing_expressions'][:5]}")
        print(f"  补充后常见表达: {first['common_expressions'][:5]}")
        print(f"  所有常见表达: {get_all_expressions(first)[:5]}")
