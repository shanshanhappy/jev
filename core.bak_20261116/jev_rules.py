# -*- coding: utf-8 -*-
"""jev_rules.py  共享规则层(五方案公用, 与具体模型无关)
职责(数据处理方案 v2.0 §4):
  1) split_segments: 解析 full_text 的【来源标记】分段 -> 证据定位域
  2) find_evidence : 词库命中关键词回搜 -> (证据字段, 位置, 命中词)
  3) sentiment     : 命中点±near 窗口共现正/负向词表 -> 正向/负向/混合/不适用
规则全部来自 标签体系词库 的四张词表 + near rule, 确定性可复现。
"""
import re
SEGS = ['标题', '正文', '图片文字', '视频花字', '语音转写']
SEG_RE = re.compile('【(' + '|'.join(SEGS) + ')】')
FIELD_MAP = {'标题': 'title', '正文': 'content', '图片文字': 'cover_ocr_content',
             '视频花字': 'video_highlight_content', '语音转写': 'audio_asr_content'}


def split_segments(text):
    ms = list(SEG_RE.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append((m.group(1), m.end(), end))
    return out


def find_evidence(text, segs, hits):
    """返回 (段名, 绝对位置, 命中词) 或 (None,None,None)=语义命中未定位"""
    for kw in sorted(hits or [], key=len, reverse=True):
        for name, s, e in segs:
            p = text.find(kw, s, e)
            if p >= 0:
                return name, p, kw
    return None, None, None


def sentiment(text, pos, kw, L):
    w = text[max(0, pos - L['near']): pos + len(kw) + L['near']]
    hp = any(x in w for x in L['poss'])
    hn = any(x in w for x in L['negs'])
    return '混合' if (hp and hn) else '正向' if hp else '负向' if hn else '不适用'


def is_ttl(L):
    """TTL/集合为汇总节点，金标准不打标，不进入问题池。"""
    return 'TTL' in L['label']


def merge_label_rows(rows):
    """按(类别,细分类别,TOPIC ANGLE)合并词库行。
    返回按首次出现顺序排列的聚合标签列表：
      idx     = 组内最小行号，作为qid与输出代表行；
      idxs    = 组内全部原始行号，用于评测回溯；
      members = 组内原始行，B模式粗筛任一行命中即可；
      hits/ands/excls/poss/negs = 组内有序去重并集。
    这样避免同名标签被重复提问，同时保留全部关键词用于证据回搜。
    """
    groups = {}
    for L in rows:
        if is_ttl(L):
            continue
        key = (L['cat'], L['sub'], L['label'])
        if key not in groups:
            g = dict(L)
            g['idxs'] = []
            g['members'] = []
            for fld in ('hits', 'ands', 'excls', 'poss', 'negs'):
                g[fld] = []
            groups[key] = g
        g = groups[key]
        g['idxs'].append(L['idx'])
        g['members'].append(L)
        for fld in ('hits', 'ands', 'excls', 'poss', 'negs'):
            for w in L[fld]:
                if w not in g[fld]:
                    g[fld].append(w)
        g['near'] = max(g['near'], L['near'])
    return list(groups.values())
