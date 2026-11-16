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
                       screen_label, q_instruction, est_tokens)

# 全局措辞风格, 由 main() 从 --qstyle 设置(v1=旧确认式, v2=判据式)
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
        if not only or r['样本号'] in only:
            want[int(r['CSV记录序号'])] = r['样本号']
            reason[r['样本号']] = (r.get('入样原因') or '').strip()

    v3 = {}
    for row in csv.DictReader(open(V3F, encoding='utf-8-sig', newline='')):
        n = int(row['csv_row_no'])
        if n in want:
            v3[want[n]] = row
    return labels, v3, reason, max_chars


def make_payload(sample, v3_row, questions, stage, scheme, mode, budget=55000, model='jev-latest', max_chars=2000, reason=''):
    """构建请求体, 自动分块。

    budget 是**单请求总预算**(含 state)。每个 chunk 都要重复携带同一份 state,
    所以切块前先把 state 的 token 从预算里扣掉。
    """
    text_full = v3_row['full_text'] or ''
    text = text_full[:max_chars]
    truncated = len(text_full) > max_chars
    state = {'平台': v3_row['site_name'], '内容': text}
    state_t = est_tokens(json.dumps(state, ensure_ascii=False))
    avail = max(2000, budget - state_t)

    chunks, cur, cur_t = [], {}, 0
    for qid, q in questions.items():
        t = est_tokens(json.dumps({qid: q}, ensure_ascii=False))
        if cur and cur_t + t > avail:
            chunks.append(cur); cur, cur_t = {}, 0
        cur[qid] = q; cur_t += t
    if cur:
        chunks.append(cur)

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
                'reason': reason
            }
        })
    return payloads


def write_payloads(payloads, scheme):
    """写入 jsonl 文件（按样本分别落盘，避免多样本挤进同一文件）"""
    os.makedirs(OUTD, exist_ok=True)
    if not payloads:
        return []

    by_sample = {}
    for p in payloads:
        by_sample.setdefault(p['sample'], []).append(p)

    paths = []
    for sample in sorted(by_sample):
        group = by_sample[sample]
        mode = group[0]['mode']
        fn = '%s__%s__%s.jsonl' % (sample, scheme, mode)
        path = os.path.join(OUTD, fn)
        with open(path, 'w', encoding='utf-8') as f:
            for p in group:
                f.write(json.dumps(p, ensure_ascii=False) + '\n')
        paths.append(path)

        text = group[0]['state']['内容']
        with open(os.path.join(OUTD, '%s.text.txt' % sample), 'w', encoding='utf-8') as f:
            f.write(text)
        meta = dict(group[0]['meta'])
        meta['sample'] = sample
        meta['scheme'] = scheme
        meta['chunks'] = len(group)
        with open(os.path.join(OUTD, '%s.meta.json' % sample), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False)

    # 生成日志：记批次，让 out/payloads 里的东西可追溯（不花钱、不影响结果）
    try:
        import genlog
        nq = sum(len(p['questions']) for p in payloads)
        genlog.record('core/schemes.py', '生成请求体', OUTD, 'dir', len(paths),
                      note='每套 3 个文件（请求体/正文/meta），重跑同名覆盖',
                      params={'方案': scheme, '问法': payloads[0]['mode'], 'qstyle': QSTYLE,
                              '请求数': len(payloads), '问题合计': nq})
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

            # Step 2: 全部标签 (实际运行时会根据 gate_cat 结果过滤, 这里先全量生成)
            leaf_questions = {'q%04d' % L['idx']: make_a2_question(L) for L in labels}
            payloads_s2 = make_payload(s, v3[s], leaf_questions, 'leaf', 'H', 'cascade', args.budget, args.model, max_chars, reason[s])

            all_payloads.extend(payloads_s1 + payloads_s2)
            print('%s: 类别%d问 + 标签%d问 -> %d块' % (s, len(cat_questions), len(leaf_questions), len(payloads_s1) + len(payloads_s2)))

        if all_payloads:
            write_payloads(all_payloads, 'H')


# ============================================================
# 方案 RAG: 语义检索后 Jev 验证
# ============================================================
class SchemeRAG:
    @staticmethod
    def similarity(text, L):
        if not L['hits']:
            return 0.0
        hits = sum(1 for h in L['hits'] if h in text)
        return hits / len(L['hits'])

    @staticmethod
    def run(labels, v3, reason, max_chars, args):
        print('方案 RAG: 语义检索后 Jev 验证 (Top-K)')
        all_payloads = []
        for s in sorted(v3):
            text = (v3[s]['full_text'] or '')[:max_chars]
            scored = [(SchemeRAG.similarity(text, L), L) for L in labels]
            scored.sort(key=lambda x: -x[0])
            top = [(sc, L) for sc, L in scored[:args.top_k] if sc > 0]
            if not top:
                print('%s: 无相似标签' % s)
                continue
            questions = {'q%04d' % L['idx']: make_a2_question(L) for _, L in top}
            payloads = make_payload(s, v3[s], questions, 'leaf', 'RAG', 'similarity', args.budget, args.model, max_chars, reason[s])
            all_payloads.extend(payloads)
            print('%s: Top-%d相似 -> %d块' % (s, len(top), len(payloads)))
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
        print('方案 Hybrid: 正则+语义 (正则命中直接用, 语义标签调Jev)')
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
        all_payloads = []
        for s in sorted(v3):
            # Pass 1: A1全量
            questions_p1 = {'q%04d' % L['idx']: make_a1_question(L) for L in labels}
            payloads_p1 = make_payload(s, v3[s], questions_p1, 'pass1', 'Speculative', 'speculative', args.budget, args.model, max_chars, reason[s])

            # Pass 2: A2全量 (实际运行时会过滤边界标签)
            questions_p2 = {'q%04d' % L['idx']: make_a2_question(L) for L in labels}
            payloads_p2 = make_payload(s, v3[s], questions_p2, 'pass2', 'Speculative', 'speculative', args.budget, args.model, max_chars, reason[s])

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
    ap.add_argument('--cat-tau', type=float, default=0.15, help='H方案类别门控阈值')
    ap.add_argument('--cat-k', type=int, default=12, help='H方案类别Top-K')
    ap.add_argument('--lo', type=float, default=0.2, help='Speculative边界下限')
    ap.add_argument('--hi', type=float, default=0.8, help='Speculative边界上限')
    ap.add_argument('--qstyle', default='v2', choices=['v1', 'v2'],
                    help='问题措辞: v2=判据式(默认) / v1=旧确认式(做A/B对照)')
    ap.add_argument('--excls-scope', default='window', choices=['window', 'full', 'off'],
                    help='B方案排除词判定: window=命中点±near(默认) / full=全文(旧行为) / off=不看')
    ap.add_argument('--outdir', default='',
                    help='payload 输出目录; 空=out\\payloads。多方案并行时建议每方案一个目录, '
                         '如 --outdir out\\payloads_A1 (否则 meta.json/text.txt 会互相覆盖)')
    args = ap.parse_args()
    QSTYLE = args.qstyle
    if args.outdir:
        OUTD = os.path.abspath(args.outdir)

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
