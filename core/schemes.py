# -*- coding: utf-8 -*-
"""
schemes.py - 所有 Jev 打标方案的统一实现
每个方案一个类, 自包含不依赖外部模块(除标准库和 jev_rules)

使用:
    python core/schemes.py --scheme A2 --samples S001,S002
    python core/schemes.py --scheme B --samples S001
    python core/schemes.py --scheme H --samples S001 --cat-tau 0.15
    python core/schemes.py --scheme RAG --samples S001 --top-k 50
    python core/schemes.py --scheme Hybrid --samples S001
    python core/schemes.py --scheme Speculative --samples S001 --lo 0.2 --hi 0.8

mode 字段(产物文件名第 3 段 `{样本}__{方案}__{模式}.jsonl`, raw 同名):
    mode 的原义是**问法粒度**, 不是运行模式。它不是命令行参数, 由各 Scheme*.run()
    在调用 make_payload() 时写死传入。当前全项目只有以下 7 个取值:
        'A1'           A1 方案: 只给标签名+类别, 不给词库(测 Jev 裸语义上限)
        'A2'           A2 方案: 带词库命中词/排除词的判据式问法(效果基准)
                       B 方案也用 'A2': B 只是用正则少问一些标签, 问法没变,
                       所以产物是 {样本}__B__A2.jsonl —— 这是唯一 scheme≠mode 的方案
        'cascade'      H 方案: 先问类别再问标签的两阶段流程
        'similarity'   RAG 方案: 按词命中率排序取 Top-K
        'hybrid'       Hybrid 方案: 正则命中 + 语义标签送问
        'speculative'  Speculative 方案: 两轮(A1 全量 -> A2 边界)
    下游依赖 mode 的 3 处(改语义必须同步):
        jev_client.py:246           拼 raw 文件名
        jev_eval.py:81,94           load_probs(scheme, mode) 的第二维筛选
        tools/raw_to_csv.py:151,160 raw 与 payload 问题文本对齐的 join key
    已知问题:
        1) 除 B 外 mode 与 scheme 一一对应, 文件名第 2/3 段是重复的;
        2) 措辞版本(qstyle: v1/v2/v3/v4)才是现在真正在变的问法维度, 但它不在文件名里,
           只写在 .meta.json 和 raw 记录里。换 qstyle 必须换 --outdir, 否则
           同名 raw / 同名评测报告会互相覆盖。

2026-11-16 修订(问题: B 方案召回 34.5%, 准确率 49.5%):
  1) 粗筛排除词改为窗口判否(scope=window), 旧版全文判否会误杀 1,731 个候选
  2) 粗筛改用"截断后文本", 与真正送模型的文本一致(旧版用全文,
     导致在第 3000 字粗筛命中、模型只看前 2000 字, 必然判 0)
  3) 问题措辞 v2: 判据式, 替代旧的确认式("是否提到了")以减少假阳性
  4) --budget 改为扣除 state 后的可用额度(旧版只算 questions)
  5) est_tokens 乘实测校准系数 ×1.10(旧版低估 10.5%)
"""
import argparse, csv, json, os, re, sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')
csv.field_size_limit(10 * 1024 * 1024)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEXF = os.path.join(ROOT, 'data', 'label_lexicon.json')
V3F = os.path.join(ROOT, 'data', 'data_export_clean_v3.csv')
LISTF = os.path.join(ROOT, 'data', '抽样清单.csv')
OUTD = os.path.join(ROOT, 'out', 'payloads')

from jev_rules import (split_segments, find_evidence, sentiment, merge_label_rows,
                       screen_label, q_instruction, est_tokens, JUDGE_PREAMBLE_V4)

# 全局措辞风格, 由 main() 从 --qstyle 设置(v1=旧确认式, v2=字面判据, v3=分档判据, v4=规则判据)
# v3 未做 A/B 验证前不作默认: A2 全量问句 tok 会从 64.4k 涨到 144.5k(+125%),
# 且测试方案 §4 要求验收前不频繁改提示词, 默认保持 v2 以与现有 100 条基线可比。
# v4 把《标注规则》的判定口径注入 state 总则(JUDGE_PREAMBLE_V4)+ 问句边界句,
# 换措辞必须换 --outdir, 否则与旧 raw/评测互相覆盖。
QSTYLE = 'v2'


# ============================================================
# 工具函数
# ============================================================

def load_data(samples=None, max_chars=2000, list_path=None):
    """加载词库、抽样清单、v3文本"""
    lex = json.load(open(LEXF, encoding='utf-8'))
    labels = merge_label_rows(lex['labels'])  # 去重后的638个标签

    want = {}
    reason = {}
    only = set(x.strip() for x in (samples or '').split(',') if x.strip())
    list_file = list_path or LISTF
    for r in csv.DictReader(open(list_file, encoding='utf-8-sig')):
        if only and r.get('样本号') not in only:
            continue
        raw = (r.get('CSV记录序号') or '').strip()
        if not raw.isdigit():
            print('!! 跳过清单行(CSV记录序号非数字): %s' % (r.get('样本号') or r))
            continue
        want[int(raw)] = r['样本号']
        reason[r['样本号']] = (r.get('入样原因') or '').strip()

    v3 = {}
    bad_rows = 0
    for row in csv.DictReader(open(V3F, encoding='utf-8-sig', newline='')):
        try:
            n = int(row['csv_row_no'])
        except (KeyError, ValueError):
            bad_rows += 1
            continue
        if n in want:
            v3[want[n]] = row
    if bad_rows:
        print('!! v3 有 %d 行 csv_row_no 非数字, 已跳过' % bad_rows)

    # 清单里有、v3 里没有 = 抽样账本与清洗产物脱节, 不能静默少几个样本
    missing = [s for n, s in sorted(want.items()) if s not in v3]
    if missing:
        print('!! %d 个样本在 v3 中找不到, 已跳过: %s%s'
              % (len(missing), ','.join(missing[:10]), '...' if len(missing) > 10 else ''))
    return labels, v3, reason, max_chars


def build_chunks(state, questions, budget):
    """把 questions 切成若干块, 保证**每块单独序列化后**不超 budget。

    分两遍:
      1) 按单问累加粗切(快)。单问估算只含自己那对引号, 不含块级的逗号/花括号,
         实测会低估约 4% —— 旧实现只做这一步, 导致 A2 全部 100 个样本的第一块
         实际 55,225~55,280 tok, 全部越过 55,000 预算。
      2) 逐块用真实块级 JSON 重估, 超预算就从尾部退若干问丢给下一块。

    返回 list[dict], 每个 dict 是一块的 questions。
    """
    items = list(questions.items())
    if not items:
        return []

    def real_tok(qs):
        return est_tokens(json.dumps({'state': state, 'questions': qs}, ensure_ascii=False))

    avail = max(2000, budget - est_tokens(json.dumps(state, ensure_ascii=False)))

    chunks, cur, cur_t = [], [], 0
    for qid, q in items:
        t = est_tokens(json.dumps({qid: q}, ensure_ascii=False))
        if cur and cur_t + t > avail:
            chunks.append(cur); cur, cur_t = [], 0
        cur.append((qid, q)); cur_t += t
    if cur:
        chunks.append(cur)

    # 第二遍: 块级校验。按"每问均值"先估要退几个再逐个微调, 避免逐问线性回退
    out, queue = [], list(chunks)
    while queue:
        ch = queue.pop(0)
        real = real_tok(dict(ch))
        if real <= budget:
            # 合规块：若与下一块合并后仍合规就合并, 否则单独成块。
            # 不合并的话, 上一块退回的几个问会自己变成一个小碎块(实测 3 问一块)。
            if queue and real_tok(dict(ch + queue[0])) <= budget:
                queue[0] = ch + queue[0]
            else:
                out.append(ch)
            continue
        if len(ch) == 1:
            print('!! 单个问题即超预算(%d > %d), 无法再切: %s' % (real, budget, ch[0][0]))
            out.append(ch)
            continue
        avg = max(1.0, real / len(ch))
        n = max(1, len(ch) - int((real - budget) / avg) - 1)
        while n > 1 and real_tok(dict(ch[:n])) > budget:
            n -= 1
        while n < len(ch) and real_tok(dict(ch[:n + 1])) <= budget:
            n += 1
        out.append(ch[:n])
        if queue:
            queue[0] = ch[n:] + queue[0]
        else:
            queue.insert(0, ch[n:])

    return [dict(c) for c in out]


def make_payload(sample, v3_row, questions, stage, scheme, mode, budget=55000, model='jev-latest', max_chars=2000, reason=''):
    """构建请求体, 自动分块。

    budget 是**单请求总预算**(含 state)。每个 chunk 都要重复携带同一份 state,
    所以切块前先把 state 的 token 从预算里扣掉。

    stage : 流程阶段。单阶段方案固定 'leaf'; H 为 'gate_cat'/'leaf';
            Speculative 为 'pass1'/'pass2'。多阶段时才会进文件名后缀。
    scheme: 方案名(A1/A2/B/H/RAG/Hybrid/Speculative), 决定"问哪些标签"。
    mode  : 问法粒度, 不是运行模式。由各 Scheme*.run() 写死传入:
            'A1'=裸标签 / 'A2'=带词库判据(B 方案也用 A2) /
            'cascade'|'similarity'|'hybrid'|'speculative'=对应流程名。
            取值全表见文件头 "mode 字段" 说明。
    """
    text_full = v3_row['full_text'] or ''
    text = text_full[:max_chars]
    truncated = len(text_full) > max_chars
    state = {'平台': v3_row['site_name'], '内容': text}
    if QSTYLE == 'v4':
        # v4 规则判据: 《标注规则》的通用判定口径放 state, 每块只带一次,
        # 不在 638 个问句里重复(否则 +25k tok/帖)。est/chunk 预算自动把它算进去。
        state = {'判断口径': JUDGE_PREAMBLE_V4, **state}
    chunks = build_chunks(state, questions, budget)

    payloads = []
    for i, ch in enumerate(chunks, 1):
        payloads.append({
            'sample': sample, 'mode': mode, 'stage': stage, 'scheme': scheme,
            'chunk': i, 'nchunks': len(chunks), 'model': model,
            'state': state, 'questions': ch,
            'meta': {
                'est_tokens': est_tokens(json.dumps({'state': state, 'questions': ch}, ensure_ascii=False)),
                'n_q': len(ch), 'truncated': truncated,
                'csv_row_no': int(v3_row['csv_row_no']),
                'id': v3_row['id'], 'site_name': v3_row['site_name'],
                'url': v3_row['url'], 'publish_time': v3_row['publish_time'],
                'char_count_raw': int(v3_row['char_count_raw']),
                'char_count_full': int(v3_row['char_count_full']),
                'reason': reason,
                'stage': stage,
                # 蒸馏用的可追溯字段: 换预算/换截断长度会改变块的划分与内容覆盖,
                # 不落盘就分不清两批产物为什么不同(实测最长帖 51,742 字只喂 2,000 字)。
                'budget': budget, 'max_chars': max_chars,
                'truncate_ratio': round(len(text) / len(text_full), 4) if text_full else 0.0,
                # 措辞版本必须落盘: raw 文件名只有 {样本}__{方案}__{问法}, 不含 qstyle,
                # 不记就分不清这批概率是 v2 还是 v3 产出的(不可比)。
                'qstyle': QSTYLE
            }
        })
    return payloads


def write_payloads(payloads, scheme):
    """按 (样本, stage) 落盘。

    单 stage 方案(A1/A2/B/RAG/Hybrid)沿用 `{样本}__{方案}__{模式}.jsonl`;
    多 stage 方案(H/Speculative)加 stage 后缀, 否则两个阶段的块会挤进同一文件,
    且 meta 只能记到最后覆盖的那一个阶段的 chunks —— 两种阶段的块数无法区分。
    """
    os.makedirs(OUTD, exist_ok=True)
    if not payloads:
        return []

    by_sample = {}
    for p in payloads:
        by_sample.setdefault(p['sample'], []).append(p)

    paths = []
    for sample in sorted(by_sample):
        group = by_sample[sample]

        by_stage = {}
        for p in group:
            by_stage.setdefault(p.get('stage', 'leaf'), []).append(p)
        multi = len(by_stage) > 1

        stage_meta = {}
        for st, sg in by_stage.items():
            # 文件名 = {样本}__{方案}__{问法}[__{阶段}].jsonl
            #   scheme: A1/A2/B/H/RAG/Hybrid/Speculative —— 问哪些标签
            #   mode  : A1/A2/cascade/similarity/hybrid/speculative —— 怎么问
            #           (B 方案借 A2 的问法, 所以是 __B__A2; 取值全表见文件头)
            #   stage : 仅多阶段方案才有后缀(gate_cat/leaf/pass1/pass2), 单阶段不加
            # 注意: qstyle(v1/v2/v3)不在文件名里, 换措辞必须换 --outdir
            mode = sg[0]['mode']
            fn = '%s__%s__%s%s.jsonl' % (sample, scheme, mode, ('__' + st) if multi else '')
            path = os.path.join(OUTD, fn)
            with open(path, 'w', encoding='utf-8') as f:
                for p in sg:
                    f.write(json.dumps(p, ensure_ascii=False) + '\n')
            paths.append(path)
            stage_meta[st] = {
                'file': fn, 'chunks': len(sg),
                'n_q': sum(len(p['questions']) for p in sg),
                'est_tokens': sum(p['meta']['est_tokens'] for p in sg),
            }

        text = group[0]['state']['内容']
        with open(os.path.join(OUTD, '%s.text.txt' % sample), 'w', encoding='utf-8') as f:
            f.write(text)
        meta = dict(group[0]['meta'])
        meta['sample'] = sample
        meta['scheme'] = scheme
        meta['chunks'] = len(group)
        # 顶层 n_q 记该样本所有阶段的问数合计。若照抄第一块 meta, A2 会写成 472(第一块),
        # 而实际该帖问了 638 个 —— 块级问数看各 chunk 自己的 meta.n_q。
        meta['n_q'] = sum(len(p['questions']) for p in group)
        if multi:
            meta['stages'] = stage_meta
        with open(os.path.join(OUTD, '%s.meta.json' % sample), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False)

    # 生成日志：记批次，让输出目录里的东西可追溯（不花钱、不影响结果）
    try:
        import genlog
        nq = sum(len(p['questions']) for p in payloads)
        stages = []
        for p in payloads:
            st = p.get('stage', 'leaf')
            if st not in stages:
                stages.append(st)
        genlog.record('core/schemes.py', '生成请求体', OUTD, 'dir', len(paths),
                      note='每套 3 个文件（请求体/正文/meta），重跑同名覆盖；多阶段方案另加 stage 后缀',
                      params={'方案': scheme, '问法': payloads[0]['mode'], 'qstyle': QSTYLE,
                              '阶段': ','.join(stages), '请求数': len(payloads), '问题合计': nq})
    except Exception as _e:
        print('!! 生成日志写入失败(不影响结果): %s' % _e)
    return paths


def make_a1_question(L):
    # 官方 API 要求 questions 的值是对象；EdgeOne 网关容忍纯字符串，官方会报 422
    return {'type': 'noul', 'instructions': q_instruction(L, 'A1', QSTYLE)}


def make_a2_question(L):
    return {'type': 'noul', 'instructions': q_instruction(L, 'A2', QSTYLE)}


# ============================================================
# 方案 A1: 纯语义全量
# ============================================================
class SchemeA1:
    @staticmethod
    def run(labels, v3, reason, max_chars, args):
        print('方案 A1: 纯语义全量 %d 问' % len(labels))
        all_payloads = []
        for s in sorted(v3):
            questions = {'q%04d' % L['idx']: make_a1_question(L) for L in labels}
            payloads = make_payload(s, v3[s], questions, 'leaf', 'A1', 'A1', args.budget, args.model, max_chars, reason[s])
            all_payloads.extend(payloads)
            print('%s: %d问 -> %d块' % (s, len(questions), len(payloads)))
        if all_payloads:
            write_payloads(all_payloads, 'A1')


# ============================================================
# 方案 A2: 全量+词库提示 (当前推荐)
# ============================================================
class SchemeA2:
    @staticmethod
    def run(labels, v3, reason, max_chars, args):
        print('方案 A2: 全量+词库提示 %d 问' % len(labels))
        all_payloads = []
        for s in sorted(v3):
            questions = {'q%04d' % L['idx']: make_a2_question(L) for L in labels}
            payloads = make_payload(s, v3[s], questions, 'leaf', 'A2', 'A2', args.budget, args.model, max_chars, reason[s])
            all_payloads.extend(payloads)
            print('%s: %d问 -> %d块' % (s, len(questions), len(payloads)))
        if all_payloads:
            write_payloads(all_payloads, 'A2')


# ============================================================
# 方案 B: 正则粗筛后 Jev 验证
# ============================================================
class SchemeB:
    @staticmethod
    def regex_match(text, L, scope='window'):
        return screen_label(text, L, scope=scope)

    @staticmethod
    def run(labels, v3, reason, max_chars, args):
        scope = getattr(args, 'excls_scope', 'window')
        print('方案 B: 正则粗筛后 Jev 验证 (排除词判定=%s, 粗筛文本=前%d字)' % (scope, max_chars))
        all_payloads = []
        for s in sorted(v3):
            # 粗筛必须用"真正送进模型的那段文本", 否则会问出模型看不见的标签
            text = (v3[s]['full_text'] or '')[:max_chars]
            candidates = [L for L in labels if SchemeB.regex_match(text, L, scope)]
            if not candidates:
                print('%s: 无候选' % s)
                continue
            questions = {'q%04d' % L['idx']: make_a2_question(L) for L in candidates}
            payloads = make_payload(s, v3[s], questions, 'leaf', 'B', 'A2', args.budget, args.model, max_chars, reason[s])
            all_payloads.extend(payloads)
            print('%s: %d候选 -> %d块' % (s, len(candidates), len(payloads)))
        if all_payloads:
            write_payloads(all_payloads, 'B')


# ============================================================
# 方案 H: 层级门控 (类别→标签, 两步)
# ============================================================
class SchemeH:
    @staticmethod
    def run(labels, v3, reason, max_chars, args):
        print('方案 H: 层级门控 (类别→标签)')
        print('  两阶段动态门控: gate_cat 响应回来后, leaf 按阈值+Top-K筛选；失败回退=%s' % args.gate_fallback)
        # 构建类别索引
        cat_map = defaultdict(list)
        for L in labels:
            cat_map[L['cat']].append(L)

        all_payloads = []
        for s in sorted(v3):
            # Step 1: 类别门控
            cat_questions = {}
            for cat in sorted(cat_map.keys()):
                qid = 'cat_%s' % cat
                cat_questions[qid] = {'type': 'noul', 'instructions': '这篇社媒内容是否涉及「%s」类别下的任何话题？' % cat}
            payloads_s1 = make_payload(s, v3[s], cat_questions, 'gate_cat', 'H', 'cascade', args.budget, args.model, max_chars, reason[s])

            # Step 2: 预生成叶子候选上限；jev_client 按 gate_cat 的真实概率动态裁剪。
            leaf_questions = {'q%04d' % L['idx']: make_a2_question(L) for L in labels}
            payloads_s2 = make_payload(s, v3[s], leaf_questions, 'leaf', 'H', 'cascade', args.budget, args.model, max_chars, reason[s])
            # leaf 在客户端发送前按 gate_cat 的真实响应动态筛选；qid→类别随请求落盘，
            # 避免客户端再次读取一份可能已变化的词库。
            qid_cats = {'q%04d' % L['idx']: L['cat'] for L in labels}
            flow = {'type': 'h_leaf_filter', 'cat_tau': args.cat_tau, 'cat_k': args.cat_k,
                    'fallback': args.gate_fallback, 'qid_cats': qid_cats}
            for payload in payloads_s2:
                payload['meta']['flow'] = flow

            all_payloads.extend(payloads_s1 + payloads_s2)
            print('%s: 类别%d问 + 标签%d问 -> %d块' % (s, len(cat_questions), len(leaf_questions), len(payloads_s1) + len(payloads_s2)))

        if all_payloads:
            write_payloads(all_payloads, 'H')


# ============================================================
# 方案 RAG: 语义检索后 Jev 验证
# ============================================================
class SchemeRAG:
    @staticmethod
    def run(labels, v3, reason, max_chars, args):
        print('方案 RAG: 向量语义检索后 Jev 验证 (Top-%d)' % args.top_k)
        from embeddings import EmbeddingService, cosine, label_document

        service = EmbeddingService(args.embedding_base, args.embedding_key, args.embedding_model,
                                   args.embedding_cache, args.embedding_batch_size)
        documents = [label_document(L) for L in labels]
        label_vectors = service.embed(documents)
        print('  标签向量: %d 条, 模型=%s, 缓存=%s' %
              (len(labels), args.embedding_model, args.embedding_cache))

        all_payloads = []
        for s in sorted(v3):
            text = (v3[s]['full_text'] or '')[:max_chars]
            query_vector = service.embed([text])[0]
            scored = [(cosine(query_vector, vector), L) for vector, L in zip(label_vectors, labels)]
            scored.sort(key=lambda x: -x[0])
            top = [(sc, L) for sc, L in scored[:args.top_k]
                   if args.min_score is None or sc >= args.min_score]
            if not top:
                print('%s: 无达到相似度要求的候选标签' % s)
                continue
            questions = {'q%04d' % L['idx']: make_a2_question(L) for _, L in top}
            payloads = make_payload(s, v3[s], questions, 'leaf', 'RAG', 'similarity', args.budget, args.model, max_chars, reason[s])
            score_map = {'q%04d' % L['idx']: round(sc, 6) for sc, L in top}
            for payload in payloads:
                payload['meta']['rag'] = {'embedding_model': args.embedding_model,
                                          'top_k': args.top_k, 'min_score': args.min_score,
                                          'candidate_scores': score_map}
            all_payloads.extend(payloads)
            print('%s: Top-%d 语义候选 -> %d块 (最高分 %.4f)' %
                  (s, len(top), len(payloads), top[0][0]))
        if all_payloads:
            write_payloads(all_payloads, 'RAG')


# ============================================================
# 方案 Hybrid: 正则+语义
# ============================================================
class SchemeHybrid:
    @staticmethod
    def is_semantic_only(L):
        if not L['hits']:
            return False
        label = L['label']
        return not any(label in h or h in label for h in L['hits'])

    @staticmethod
    def run(labels, v3, reason, max_chars, args):
        print('方案 Hybrid: 正则+语义 (只把语义标签送 Jev)')
        print('  !! 正则命中的标签目前不写入任何文件: 预测管道只读 raw 概率, '
              '所以 Hybrid 缺"正则直接判是"这一半。')
        all_payloads = []
        for s in sorted(v3):
            text = (v3[s]['full_text'] or '')[:max_chars]
            semantic_labels = [L for L in labels if SchemeHybrid.is_semantic_only(L)]
            if not semantic_labels:
                print('%s: 无语义标签, 正则全命中' % s)
                continue
            questions = {'q%04d' % L['idx']: make_a2_question(L) for L in semantic_labels}
            payloads = make_payload(s, v3[s], questions, 'leaf', 'Hybrid', 'hybrid', args.budget, args.model, max_chars, reason[s])
            all_payloads.extend(payloads)
            print('%s: 语义标签%d个 -> %d块' % (s, len(semantic_labels), len(payloads)))
        if all_payloads:
            write_payloads(all_payloads, 'Hybrid')


# ============================================================
# 方案 Speculative: 两轮 Jev (A1全量 + A2边界)
# ============================================================
class SchemeSpeculative:
    @staticmethod
    def run(labels, v3, reason, max_chars, args):
        print('方案 Speculative: 两轮 Jev (A1全量 + A2边界)')
        print('  两轮动态复问: pass2 只保留 pass1 概率在 [%.2f, %.2f] 的标签；失败回退=%s' % (args.lo, args.hi, args.spec_fallback))
        all_payloads = []
        for s in sorted(v3):
            # Pass 1: A1全量
            questions_p1 = {'q%04d' % L['idx']: make_a1_question(L) for L in labels}
            payloads_p1 = make_payload(s, v3[s], questions_p1, 'pass1', 'Speculative', 'speculative', args.budget, args.model, max_chars, reason[s])

            # Pass 2: 预生成 A2 候选上限；jev_client 只发送 pass1 落在 [lo, hi] 的标签。
            questions_p2 = {'q%04d' % L['idx']: make_a2_question(L) for L in labels}
            payloads_p2 = make_payload(s, v3[s], questions_p2, 'pass2', 'Speculative', 'speculative', args.budget, args.model, max_chars, reason[s])
            flow = {'type': 'speculative_pass2_filter', 'lo': args.lo, 'hi': args.hi,
                    'fallback': args.spec_fallback}
            for payload in payloads_p2:
                payload['meta']['flow'] = flow

            all_payloads.extend(payloads_p1 + payloads_p2)
            print('%s: Pass1=%d块 + Pass2=%d块' % (s, len(payloads_p1), len(payloads_p2)))

        if all_payloads:
            write_payloads(all_payloads, 'Speculative')


# ============================================================
# 主函数
# ============================================================
SCHEMES = {
    'A1': SchemeA1,
    'A2': SchemeA2,
    'B': SchemeB,
    'H': SchemeH,
    'RAG': SchemeRAG,
    'Hybrid': SchemeHybrid,
    'Speculative': SchemeSpeculative,
}


def main():
    global QSTYLE, OUTD
    ap = argparse.ArgumentParser('Jev 打标方案统一入口')
    ap.add_argument('--scheme', required=True, choices=list(SCHEMES.keys()), help='选择方案')
    ap.add_argument('--samples', default='', help='逗号分隔样本号; 空=全部')
    ap.add_argument('--list', default='', help='抽样清单CSV路径; 默认 data\\抽样清单.csv')
    ap.add_argument('--max-chars', type=int, default=2000)
    ap.add_argument('--budget', type=int, default=55000, help='单请求总预算(含 state)')
    ap.add_argument('--model', default='jev-latest')
    ap.add_argument('--top-k', type=int, default=50, help='RAG方案取Top-K')
    ap.add_argument('--cat-tau', type=float, default=0.15, help='H方案类别门控阈值，默认0.15')
    ap.add_argument('--cat-k', type=int, default=12, help='H方案额外保留概率Top-K类别，默认12')
    ap.add_argument('--gate-fallback', choices=['all', 'none'], default='all',
                    help='H类别阶段失败时：all=回退全量保召回(默认) / none=不再问叶子')
    ap.add_argument('--lo', type=float, default=0.2, help='Speculative边界下限，默认0.2')
    ap.add_argument('--hi', type=float, default=0.8, help='Speculative边界上限，默认0.8')
    ap.add_argument('--spec-fallback', choices=['none', 'all'], default='none',
                    help='Speculative第一轮失败时：none=跳过第二轮(默认) / all=第二轮全量')
    ap.add_argument('--embedding-base', default=os.environ.get('EMBEDDING_BASE_URL', 'https://api.openai.com'),
                    help='RAG向量接口，OpenAI兼容；默认EMBEDDING_BASE_URL或OpenAI')
    ap.add_argument('--embedding-key', default=os.environ.get('EMBEDDING_API_KEY', os.environ.get('OPENAI_API_KEY', '')),
                    help='RAG向量key；默认EMBEDDING_API_KEY或OPENAI_API_KEY')
    ap.add_argument('--embedding-model', default=os.environ.get('EMBEDDING_MODEL', 'text-embedding-3-small'),
                    help='RAG向量模型，默认text-embedding-3-small')
    ap.add_argument('--embedding-cache', default=os.path.join(ROOT, 'out', 'rag_embedding_cache.json'),
                    help='RAG向量缓存路径（只存哈希键与向量，不存原文）')
    ap.add_argument('--embedding-batch-size', type=int, default=32, help='向量接口每批文本数，默认32')
    ap.add_argument('--min-score', type=float, default=None, help='RAG可选最低余弦相似度；默认只看Top-K')
    ap.add_argument('--qstyle', default='v2', choices=['v1', 'v2', 'v3', 'v4'],
                    help='问题措辞: v2=字面判据(默认) / v4=规则判据(对齐《标注规则》口径, '
                         'state带判断总则+易混标签边界句) / v3=分档判据(未验证, tok +125%%) / '
                         'v1=旧确认式(做A/B对照)')
    ap.add_argument('--excls-scope', default='window', choices=['window', 'full', 'off'],
                    help='B方案排除词判定: window=命中点±near(默认) / full=全文(旧行为) / off=不看')
    ap.add_argument('--outdir', default='',
                    help='payload 输出目录; 空=out\\payloads_{方案}（按方案隔离, 否则同一目录下 '
                         '{样本}.meta.json / .text.txt 会被不同方案互相覆盖）')
    args = ap.parse_args()

    for name in ('cat_tau', 'lo', 'hi'):
        value = getattr(args, name)
        if not 0 <= value <= 1:
            ap.error('--%s 必须在 0~1 之间' % name.replace('_', '-'))
    if args.cat_k < 0 or args.top_k <= 0 or args.embedding_batch_size <= 0:
        ap.error('--cat-k 必须≥0，--top-k/--embedding-batch-size 必须>0')
    if args.lo > args.hi:
        ap.error('--lo 不能大于 --hi')
    if args.scheme == 'RAG' and not args.embedding_key:
        ap.error('RAG 需要向量 API key：设 EMBEDDING_API_KEY/OPENAI_API_KEY 或传 --embedding-key')

    QSTYLE = args.qstyle
    OUTD = (os.path.abspath(args.outdir) if args.outdir
            else os.path.join(ROOT, 'out', 'payloads_%s' % args.scheme))

    labels, v3, reason, max_chars = load_data(args.samples, args.max_chars, args.list or None)
    if not v3:
        print('!! 无匹配样本')
        return 1

    print('措辞=%s  预算=%d(含state)  截断=%d字' % (args.qstyle, args.budget, args.max_chars))
    print('输出目录=%s' % OUTD)
    SCHEMES[args.scheme].run(labels, v3, reason, args.max_chars, args)
    rel = os.path.relpath(OUTD, ROOT) if OUTD.startswith(ROOT) else OUTD
    print('\n完成: 请求体在 %s\\' % rel)
    print('下一步(务必带 --scheme，否则会把其它方案一起发出去):')
    print('    python core\\jev_client.py --dir %s --all --scheme %s'
          % (rel, args.scheme))


if __name__ == '__main__':
    sys.exit(main())
