# -*- coding: utf-8 -*-
"""jev_rules.py  共享规则层(五方案公用, 与具体模型无关)
职责(数据处理方案 v2.0 §4):
  1) split_segments: 解析 full_text 的【来源标记】分段 -> 证据定位域
  2) find_evidence : 词库命中关键词回搜 -> (证据字段, 位置, 命中词)
  3) sentiment     : 命中点±near 窗口共现正/负向词表 -> 正向/负向/混合/不适用
  4) screen_label  : B/RAG 类方案粗筛(否定词只在命中点窗口内判否, 不再全文判死)
  5) q_instruction : Noul 问题措辞统一入口(v1 旧措辞 / v2 判据措辞)
  6) est_tokens    : token 估算(已按 100 条实测校准 ×1.10)
规则全部来自 标签体系词库 的四张词表 + near rule, 确定性可复现。
"""
import re

# 通用否定词: 362 个标签共用同一套, 无区分度
# -> 不写进问题正文(写了也是废话), 只用于粗筛的窗口判否
GENERIC_NEG = {'不', '没', '没有', '没用', '并非', '不算', '毫无',
               '根本不', '一点不', '没效果', '没啥用', '毫无作用'}

# token 估算校准系数: 100 条 B 方案实测 估 220,442 / 实 243,480 = ×1.104
TOKEN_CAL = 1.10

SEGS = ['标题', '正文', '图片文字', '视频花字', '语音转写']
SEG_RE = re.compile('【(' + '|'.join(SEGS) + ')】')
FIELD_MAP = {'标题': 'title', '正文': 'content', '图片文字': 'cover_ocr_content',
             '视频花字': 'video_highlight_content', '语音转写': 'audio_asr_content'}


def est_tokens(s):
    """粗略估 token: CJK 约 1 字 1 tok, 其余 0.35; 再乘实测校准系数。
    旧版低估 10.5%, 导致 --budget 55000 实际打到约 61k。"""
    cjk = sum(1 for ch in s if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' or '\uff00' <= ch <= '\uffef')
    return int((cjk + (len(s) - cjk) * 0.35) * TOKEN_CAL)


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


def _hit_negated(text, p, kw, excls, near):
    """该命中位置是否落在否定语境里(只看 ±near 窗口)"""
    return any(ex in text[max(0, p - near): p + len(kw) + near] for ex in excls)


def screen_label(text, L, scope='window'):
    """粗筛: 只要有一个命中词出现在**非否定**语境里, 就算候选标签。

    scope='window' 命中点 ±near(词库 near 列, 默认 15 字)窗口内判否 —— 默认, 修正版
    scope='full'   全文任一处出现排除词即判死 —— 旧行为, 仅用于复现/对照
    scope='off'    不看排除词

    旧版(full)的问题: 362 个标签共用同一套通用否定词(不/没/没有/没用...),
    做全文匹配时, 中文帖子几乎都含"不"字 -> 候选被大面积误杀。
    实测 100 条: 每帖送问 39.9 -> 22.6, 共误杀 1,731 个候选标签。

    text 应传"实际送进模型的文本"(截断后的), 保证粗筛命中的词模型看得见。
    """
    if not L['hits']:
        return False
    if L['ands'] and not any(a in text for a in L['ands']):
        return False
    if scope == 'off' or not L['excls']:
        return any(h in text for h in L['hits'])
    if scope == 'full':
        for ex in L['excls']:
            if ex in text:
                return False
        return any(h in text for h in L['hits'])
    near = L.get('near') or 15
    for kw in L['hits']:
        p = text.find(kw)
        while p >= 0:
            if not _hit_negated(text, p, kw, L['excls'], near):
                return True
            p = text.find(kw, p + 1)
    return False


QSTYLES = ('v1', 'v2')


def q_instruction(L, mode='A2', style='v2', max_hits=8, max_excls=3):
    """生成 Noul 问题的 instructions。

    mode='A1'   只给标签名, 测 Jev 裸语义上限
    mode='A2'   带词库提示(默认)
    style='v1'  旧措辞, 保留做 A/B 对照:
                "这篇社媒内容是否提到了「X」？常见说法如a、b、c，同义改写也算。"
                属确认式提问, 100 条实测假阳性 691/1368
    style='v2'  判据式(默认): 主题 + 常见说法 + 正向判据 + 反例
    """
    ctx = L['cat'] if L['sub'] in ('', '通用') else '%s·%s' % (L['cat'], L['sub'])
    hits = [h for h in L['hits'] if h][:max_hits]
    excls = [e for e in L['excls'] if e][:max_excls]

    if mode == 'A1':
        return '这篇社媒内容是否提到了「%s」（%s）？' % (L['label'], ctx)

    if style == 'v1':
        ins = '这篇社媒内容是否提到了「%s」（%s）？' % (L['label'], ctx)
        if hits:
            ins += '常见说法如%s，同义改写也算。' % '、'.join(hits[:6])
        if excls:
            ins += '若语境是%s等否定/排除含义则不算。' % '、'.join(excls[:3])
        return ins

    s = '原文是否明确表达「%s」（%s）？' % (L['label'], ctx)
    if hits:
        s += '常见说法如%s。' % '、'.join(hits)
    s += '只有原文有对应的明确表述才算；只是相关话题、常识推测、隐含暗示都不算。'
    spec = [e for e in excls if e not in GENERIC_NEG]
    if spec:
        s += '若原文是%s等排除含义，也不算。' % '、'.join(spec)
    elif excls:
        s += '若原文是否定或排除含义，也不算。'
    return s


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
