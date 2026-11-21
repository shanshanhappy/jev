# -*- coding: utf-8 -*-
"""
强正则匹配逻辑 - 增强版标签体系（严格版 v5 - 修复同名标签）
核心修改：所有结果集（direct_hits, review_hits, evidence）全部以 label_id 为主键，避免同名标签被 Set 去重丢失。
"""
from typing import List, Dict, Any, Set, Tuple, Optional
from .label_loader import get_all_expressions

NEGATION_WORDS = sorted(['不', '没', '没有', '非', '未', '无', '不是', '并非', '不算', '不太', '不怎么', '不大', '不够', '不会', '不能', '不要', '别', '勿', '莫', '休'], key=len, reverse=True)
CONTRAST_WORDS = ['但', '但是', '不过', '然而', '却', '只是', '只是说']
SENTENCE_DELIMITERS = ['。', '？', '！', '；', '\n']
NEGATION_WINDOW = 15
PRODUCT_TYPES = ['霜', '乳', '液', '水', '精华', '面膜', '眼霜', '面霜', '乳液', '喷雾', '凝胶', '膏', '油', '粉', '胶', '膜', '贴', '片', '套装', '组合', '系列', '产品', '用品']

def strong_regex_match(text: str, labels: List[Dict[str, Any]], use_common_expressions: bool = True) -> Dict[str, Any]:
    # ✅ 修复：集合和字典的 key 全部改为 label_id
    result = {'direct_hits': set(), 'review_hits': set(), 'evidence': {}}
    
    for label in labels:
        label_id = label['label_id']
        label_name = label['label']
        all_expressions = get_all_expressions(label) if use_common_expressions else label.get('existing_expressions', [])
        
        label_match = _find_exact_match(text, label_name)
        if label_match:
            start, end = label_match
            if _check_compound_word(text, start, end): continue
            has_negation = _check_negation_window_v4(text, start, end)
            
            if has_negation:
                result['review_hits'].add(label_id)
                result['evidence'][label_id] = {'type': 'review', 'label_name': label_name, 'matched_text': label_name, 'position': (start, end), 'context': _get_context(text, start, end, 50)}
            else:
                result['direct_hits'].add(label_id)
                result['evidence'][label_id] = {'type': 'direct', 'label_name': label_name, 'matched_text': label_name, 'position': (start, end), 'context': _get_context(text, start, end, 50)}
        else:
            best_match = None
            for expr in all_expressions:
                if not expr or expr == label_name: continue
                match = _find_exact_match(text, expr)
                if match:
                    start, end = match
                    if _check_compound_word(text, start, end): continue
                    has_negation = _check_negation_window_v4(text, start, end)
                    if not has_negation:
                        best_match = (expr, start, end, False)
                        break
                    elif best_match is None:
                        best_match = (expr, start, end, True)
            if best_match:
                expr, start, end, has_negation = best_match
                if has_negation:
                    result['review_hits'].add(label_id)
                    result['evidence'][label_id] = {'type': 'review', 'label_name': label_name, 'matched_text': expr, 'position': (start, end), 'context': _get_context(text, start, end, 50)}
                else:
                    result['direct_hits'].add(label_id)
                    result['evidence'][label_id] = {'type': 'direct', 'label_name': label_name, 'matched_text': expr, 'position': (start, end), 'context': _get_context(text, start, end, 50)}
    return result

def _find_exact_match(text: str, pattern: str) -> Optional[Tuple[int, int]]:
    if not pattern: return None
    start = text.find(pattern)
    return (start, start + len(pattern)) if start != -1 else None

def _check_compound_word(text: str, start: int, end: int) -> bool:
    if end < len(text):
        for pt in PRODUCT_TYPES:
            if text[end:end + len(pt)] == pt: return True
    return False

def _check_negation_window_v4(text: str, start: int, end: int) -> bool:
    if _check_local_negation(text, start, end): return True
    if _check_sentence_negation(text, start, end): return True
    return False

def _check_local_negation(text: str, start: int, end: int) -> bool:
    before_text = text[max(0, start - NEGATION_WINDOW):start]
    after_text = text[end:min(len(text), end + NEGATION_WINDOW)]
    for nw in NEGATION_WORDS:
        if nw in before_text:
            neg_pos = before_text.rfind(nw)
            if not any(cw in before_text[neg_pos + len(nw):] for cw in CONTRAST_WORDS): return True
    for nw in NEGATION_WORDS:
        if nw in after_text: return True
    return False

def _check_sentence_negation(text: str, start: int, end: int) -> bool:
    sentence_start = 0
    for d in SENTENCE_DELIMITERS:
        pos = text.rfind(d, 0, start)
        if pos > sentence_start: sentence_start = pos + 1
    sentence_end = len(text)
    for d in SENTENCE_DELIMITERS:
        pos = text.find(d, end)
        if pos != -1 and pos < sentence_end: sentence_end = pos
    sentence = text[sentence_start:sentence_end]
    match_pos = start - sentence_start
    for nw in NEGATION_WORDS:
        if nw in sentence:
            neg_pos = sentence.find(nw)
            if neg_pos < match_pos:
                if not any(cw in sentence[neg_pos + len(nw):match_pos] for cw in CONTRAST_WORDS): return True
    return False

def _get_context(text: str, start: int, end: int, size: int = 50) -> str:
    return f"...{text[max(0, start-size):start]}[{text[start:end]}]{text[end:min(len(text), end+size)]}..."
