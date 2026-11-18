# -*- coding: utf-8 -*-
"""jev_rules.py  共享规则层(五方案公用, 与具体模型无关)
职责(数据处理方案 v2.0 §4):
  1) split_segments: 解析 full_text 的【来源标记】分段 -> 证据定位域
  2) find_evidence : 词库命中关键词回搜 -> (证据字段, 位置, 命中词)
  3) sentiment     : 命中点±near 窗口共现正/负向词表 -> 正向/负向/混合/不适用
  4) screen_label  : B/RAG 类方案粗筛(否定词只在命中点窗口内判否, 不再全文判死)
  5) q_instruction : Noul 问题措辞统一入口(v1 旧措辞 / v2 判据措辞(默认) / v3 分档措辞 / v4 规则判据)
  6) est_tokens    : token 估算(已按 100 条实测校准 ×1.10)
规则全部来自 标签体系词库 的四张词表 + near rule, 确定性可复现。
v4 的判断口径来自《标注规则》(v1.6/v1.7) 中可迁移到"是否提及"判定的部分。
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


QSTYLES = ('v1', 'v2', 'v3', 'v4')

# ---- v4 规则判据: 把《标注规则》的判定口径注入 Jev ----
# 实测背景(100条, tau=0.6):
#   v1 确认式: 假阳性 691/1368 (约50%) —— 放得太宽
#   v2 字面判据: "只有原文有对应的明确表述才算" 把客观提及/宣传宣称/语义等价一起判死,
#                R 锁在 77%, FN 415 条全部送问过 —— 收得太死
# v4 的修法: 口径与 LLM 金标准(《标注规则》)对齐 —— "提及"含客观提及与宣传宣称,
#   否定要区分"否定现象发生"与"否定效果评价"。通用口径放 state 总则(每块一次),
#   不放 638 个问句里重复; 易混标签用 SPECIAL_HINTS 给单条边界句。
#
# state 总则: 从《标注规则》通用规则§1/§2/§6/§8、v1.6§2、v1.7§2/§6 提炼,
# 只保留对"是否提及"是非判定有区分度的部分, 与输出契约相关的(target_role/evidence 等)
# Jev 用不上, 不注入以控制 token。
JUDGE_PREAMBLE_V4 = (
    '判断口径（对下面每个问题统一适用）：'
    '1.按语义判断，不要求逐字出现，同义改写、口语说法、谐音黑话都算；'
    '2.客观提及也算——品牌、成分、功效、肤质等被提到即算，广告宣传、他人转述同样算提及，'
    '不要求作者亲身使用，也不要求效果已验证；'
    '3.成分名出现不自动等于其功效被提及（如提到"烟酰胺"不等于提到"美白"），'
    '系列名不自动推出功效；'
    '4.否定分两类：否定问题或现象发生（"不搓泥""不刺激"）→ 该问题标签不算；'
    '否定效果评价（"没觉得淡纹""没变白"）→ 该功效标签仍算提及；'
    '5.转折、比较、混合体验按实际含义处理，不因出现否定词就整体判否，'
    '也不把对A产品的描述算到B产品头上；'
    '6.只依据原文，不凭常识推测，不把适用人群推断为作者本人。'
)

# 易混标签的边界句, 键=(cat, label)。全部来自《标注规则》已确认口径:
# 近义功效分工(规则§8)、功能vs状态(v1.7§3)、敏感肌三种角色(确认口径2/3, v1.7§4)、
# 人的衰老困扰vs产品抗老(v1.7§3)、系列绑定(v1.7§1)、内容类型互斥(规则§9, v1.7§6)、
# 复购(v1.7§5)、直播(v1.7§6)、学生身份(v1.7§4)。
SPECIAL_HINTS = {
    ('功效', '提亮肤色'): '仅当明确指向肤色变亮才算；说的是黄气、暗沉归「改善暗黄」，无法区分的泛指归「提亮」。',
    ('功效', '改善暗黄'): '仅当指向黄气、暗沉才算；明确说肤色变亮归「提亮肤色」；天生黄皮不算暗黄困扰。',
    ('功效', '提亮'): '仅用于无法归入「提亮肤色」或「改善暗黄」的泛指提亮表达。',
    ('功效', '保湿补水'): '指保湿补水这一功效维度被提及，产品功能描述、宣传宣称也算。',
    ('功效', '补水保湿'): '指补水保湿这一功效维度被提及，产品功能描述、宣传宣称也算。',
    ('效果表现', '保湿/水润'): '指皮肤实际表现出的水润状态感受；若只是产品功能或宣传描述，归功效标签。',
    ('功效', '紧致'): '指紧致这一功效维度被提及，产品功能描述、宣传宣称也算。',
    ('效果表现', '紧致'): '指皮肤实际表现出的紧致状态感受；若只是产品功能或宣传描述，归功效标签。',
    ('使用肤感', '搓泥相关'): '指护肤品本身搓泥；「不搓泥」是否定现象发生，不算；上妆时搓泥归「上妆问题·搓泥」。',
    ('使用肤感', '搓泥'): '指护肤品本身搓泥；「不搓泥」是否定现象发生，不算；上妆时搓泥归「上妆问题·搓泥」。',
    ('上妆问题', '搓泥'): '仅指上妆过程中的搓泥；护肤品本身搓泥归「使用肤感」；「不搓泥」不算。',
    ('肤质类型', '敏感肌'): '指敏感肌这一肤质话题被提及，不限定是作者本人——「适合敏感肌」「妈妈是敏感肌」都算提及。',
    ('使用肤感', '敏感肌可用'): '指具体产品「适合敏感肌、敏感肌能用」的适用性描述，广告宣称也算；不等于作者本人是敏感肌。',
    ('成分特点', '敏感肌可用'): '指具体产品「适合敏感肌、敏感肌能用」的适用性描述，广告宣称也算；不等于作者本人是敏感肌。',
    ('特定人群', '敏感肌'): '指敏感肌作为目标人群被点名（如「敏感肌姐妹看过来」）。',
    ('人群特征', '敏感肌'): '指敏感肌人群特征被提及；不能由「适合敏感肌」反推作者身份。',
    ('皮肤问题', '抗老'): '指人自身的衰老困扰或焦虑（怕老、长纹焦虑等）；产品抗老功效归「功效·抗老化」。',
    ('功效', '抗老化'): '指产品抗老功效被提及，宣传宣称也算；人的衰老情绪归「皮肤问题·抗老」。',
    ('系列', '双抗精华'): '必须有「珀莱雅」与「双抗」的关系证据才算；只说「早C晚A」不充分。',
    ('系列', '美白系列'): '须有明确的系列归属证据；普通「美白」字样或美白特证版不直接算系列提及。',
    ('内容类型', '软广'): '需出现链接、团购、优惠、返利、带货等商业合作或购买转化信号才算；无商业信号的自发推荐归「种草」；个人闲置转售不算。',
    ('内容类型', '种草'): '指无商业信号的自发推荐；有链接、团购、优惠等商业信号归「软广」。',
    ('内容类型', '测评'): '需有完整的实际测试或试用过程；纯攻略清单归「日常分享」。',
    ('内容类型', '日常分享'): '指纯攻略清单或日常记录，无完整实测过程、无商业信号。',
    ('产品对比与选择', '复购/弃用'): '指具体产品的回购或不再购买表达，作者、真实第三方或宣传口径都算；店铺复购率等纯B端数据不算。',
    ('营销动作', '直播'): '需涉及直播话题本身（看直播、直播间、直播价等）；只提到产品而无关直播不算。',
    ('特定人群', '学生党/年轻'): '需原文明确学生身份或学生场景；只说20岁或年轻不等于学生。',
    ('人群特征', '学生党'): '需原文明确学生身份或学生场景；学生身份不自动等于某种消费力。',
    ('人群特征', '平价学生党'): '需同时有学生身份与平价消费定位的证据。',
}

# ---- v3 分档依据: 标签"会不会自称" ----
# 这些类别的内容, 原帖基本不会自己点明。用"只有原文有明确表述才算"去问它们
# 等于禁答 —— 实测 A2 100 条 FN TOP15 里 软广35/平价20/种草15/测评5/直播5
# 全部出自这类标签, 不是模型判错, 是问题把答案框死了。
INFER_CATS = {
    '内容类型', '价格敏感度', '营销动作', '可信度',
    '产品推荐', '产品对比与选择', '服务物流对话',
}

# 每类"没直说时该看什么线索"。没有条目则退回通用语义判据。
INFER_HINTS = {
    '内容类型': '通篇像个人分享却不直接说广告、卖点排得整齐、只夸不贬、'
                '带购买链接或团购口令、引导去评论区/私信/主页、同一产品反复推荐',
    '价格敏感度': '出现具体价格或价位区间、强调便宜划算/性价比/学生党友好/'
                  '不心疼/可以回购, 或嫌贵、等促销、蹲活动',
    '营销动作': '提到直播间/上下架时间/优惠券/满减/赠品/预售/联名/代言/合作报备',
    '可信度': '提到奖项、检测报告、备案号、医生或专家推荐、销量榜单排名、官方认证',
    '产品推荐': '明确安利给别人、回购、囤货、求链接、评论区问哪里买',
    '产品对比与选择': '拿两个以上产品做比较、纠结选哪个、平替或替代、换掉某产品',
    '服务物流对话': '提到客服回复、快递时效、发货、退换货、售后处理',
}


def _neg_clause(excls):
    """统一的排除词尾巴, 三个 style 共用。"""
    spec = [e for e in excls if e not in GENERIC_NEG]
    if spec:
        return '若原文带「%s」这类否定或排除说法，也不算。' % '、'.join(spec)
    if excls:
        return '若原文是否定或排除的含义，也不算。'
    return ''


def _v3_semantic(L, ctx, hits, excls):
    """v3-语义型: 判据 = 能不能从原文引出一句支撑, 而不是出现某个词。"""
    s = ('原文有没有在说「%s」（%s）？不必出现这几个字，也不限于词表写法；'
         '只要能从原文引出一句具体的话支撑它，就算。') % (L['label'], ctx)
    if hits:
        s += '直接写了%s → 算；' % '、'.join(hits)
    s += ('换个说法也算：具体到部位或现象的描述、使用前后的对比、实际使用中的表现。'
          '只是泛泛相关、靠常识推测、没有具体指向（如只笼统说"显老""好用"）→ 不算；'
          '原文是否定或假设（"没有…""如果…才会"）→ 不算，除非说的是改善前后的变化。')
    return s + _neg_clause(excls)


def _v3_inferred(L, ctx, hits, excls):
    """v3-推断型: 判据 = 有没有可引用的线索, 并明确允许侧面体现。"""
    s = ('原文是否属于「%s」（%s）？这类标签原帖通常不会自己点明，'
         '不要等它出现"%s"这几个字。') % (L['label'], ctx, L['label'])
    if hits:
        s += '直接写了%s → 算；' % '、'.join(hits)
    hint = INFER_HINTS.get(L['cat'], '')
    if hint:
        s += '没直说但有这些线索之一 → 也算：%s。' % hint
    s += '只出现相关词但语境无关、或纯个人记录没有任何指向 → 不算。'
    return s + _neg_clause(excls)


def _v4_neg_clause(excls):
    """v4 只用词库自己的专属排除词; 通用否定尾巴不加 ——
    它与总则第4条冲突(「没觉得淡纹」是否定效果评价, 仍算提及),
    否定的语义统一由 JUDGE_PREAMBLE_V4 处理。"""
    spec = [e for e in excls if e not in GENERIC_NEG]
    if spec:
        return '若原文带「%s」这类否定或排除说法，也不算。' % '、'.join(spec)
    return ''


def _v4_semantic(L, ctx, hits, excls):
    """v4-会自称的标签: 语义判据 + 客观提及即算, 易混标签追加边界句。"""
    s = '原文是否提及「%s」（%s）？' % (L['label'], ctx)
    if hits:
        s += '常见说法如%s。' % '、'.join(hits)
    s += '客观提及也算，不要求亲身使用或效果验证。'
    hint = SPECIAL_HINTS.get((L['cat'], L['label']))
    if hint:
        s += hint
    s += '只是话题沾边、凭常识推测、或语境无关的不算。'
    return s + _v4_neg_clause(excls)


def _v4_inferred(L, ctx, hits, excls):
    """v4-不会自称的标签(INFER_CATS): 线索判据, 允许侧面体现, 比 v3 精简。"""
    s = ('原文是否属于「%s」（%s）？这类标签原帖通常不会自己点明，'
         '不要等它出现这几个字。') % (L['label'], ctx)
    if hits:
        s += '直接写了%s → 算；' % '、'.join(hits)
    hint = INFER_HINTS.get(L['cat'], '')
    if hint:
        s += '没直说但有这些线索之一 → 也算：%s。' % hint
    sp = SPECIAL_HINTS.get((L['cat'], L['label']))
    if sp:
        s += sp
    s += '只出现相关词但语境无关 → 不算。'
    return s + _v4_neg_clause(excls)


def q_instruction(L, mode='A2', style='v2', max_hits=8, max_excls=3):
    """生成 Noul 问题的 instructions。

    mode='A1'   只给标签名, 测 Jev 裸语义上限
    mode='A2'   带词库提示(默认)
    style='v1'  旧确认式, 保留做 A/B 对照:
                "这篇社媒内容是否提到了「X」？常见说法如a、b、c，同义改写也算。"
                100 条实测假阳性 691/1368 (约 50%)
    style='v2'  字面判据式: "只有原文有对应的明确表述才算"
                压住了假阳性, 但把语义等价和侧面体现一并判死,
                R 被锁在 77%, FN 415 条全部送问过 → 漏在判否环节
    style='v3'  分档判据(需 --qstyle v3 显式开启, 非默认):
                会自称的标签 → 语义判据(能引出一句支撑即可)
                不会自称的标签(INFER_CATS) → 线索判据(明确允许侧面体现)
                未做 A/B 验证前不作默认: A2 问句 tok 从 64.4k 涨到 144.5k(+125%)
    style='v4'  规则判据(需 --qstyle v4 显式开启): 与《标注规则》口径对齐 —
                通用判定口径进 state 总则(JUDGE_PREAMBLE_V4, 每块一次);
                问句 = 语义判据 + "客观提及即算" + 易混标签边界句(SPECIAL_HINTS);
                INFER_CATS 走精简版线索判据。
                针对 v2 的 FN 主因(客观提及/宣传宣称被误杀)与 v1 的 FP 主因(无判据)。
    """
    ctx = L['cat'] if L['sub'] in ('', '通用') else '%s·%s' % (L['cat'], L['sub'])
    hits = [h for h in L['hits'] if h][:max_hits]
    excls = [e for e in L['excls'] if e][:max_excls]

    if mode == 'A1':
        return '这篇社媒内容是否提到了「%s」（%s）？' % (L['label'], ctx)

    if style == 'v4':
        fn = _v4_inferred if L['cat'] in INFER_CATS else _v4_semantic
        return fn(L, ctx, hits, excls)

    if style == 'v3':
        # 会自称的标签 → 语义判据; 不会自称的标签 → 线索判据
        fn = _v3_inferred if L['cat'] in INFER_CATS else _v3_semantic
        return fn(L, ctx, hits, excls)

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
    return s + _neg_clause(excls)


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
