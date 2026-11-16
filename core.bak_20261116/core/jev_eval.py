# -*- coding: utf-8 -*-
r"""jev_eval.py  第5步: 原始响应 + 词库 + 金标准 -> 预测两表 + 评测报告
职责(见 docs\数据处理方案.md §10.2~10.5):
  1) 解析 out\raw\{sample}__{scheme}__{mode}.jsonl 的 Noul 概率 -> 按阈值判命中
  2) 词库反查标签四件套; 证据回搜(命中关键词->所在【来源段】与原文窗口); 情绪 near 窗口规则
  3) 输出预测 细分17列 / 汇总12列, 并与金标准按 (样本号, 标签idx) 双键对齐

用法:
  python core\jev_eval.py --scheme B --mode A2 --tau 0.6
  python core\jev_eval.py --scheme B --mode A2            # tau 取 raw_to_csv.DEFAULT_THRESHOLD
输出:
  out\打标结果细分-Jev_{scheme}_{mode}.csv   out\打标结果汇总-Jev_{scheme}_{mode}.csv
  out\评测报告_{scheme}_{mode}.md

2026-11-16 修订:
  1) --scheme 与 --mode 解耦。旧版只认 --mode, 而文件名是 {sample}__{scheme}__{mode}.jsonl,
     传 --mode B 永远匹配不到(0 条), 传 --mode A2 又会把 B 方案的数据读进来。
  2) P/R/F1 统一到 (样本号, 标签idx) 唯一对口径。旧版 TP 按"金标准行"计、FP 按"预测标签"计,
     量纲不同: 金标准同一标签被拆成多行时, 一个预测能把多个 unit 同时判对,
     TP 从 677 虚增到 1189, 准确率 49.5% 被报成 63.2%。
  3) --tau 默认改为读 tools\raw_to_csv.DEFAULT_THRESHOLD(0.6), 与打标明细表同口径。
  4) 修复 markdown 表格分隔符: FN/FP 列表原来用 "|" 连接维度与标签, 会把表格撑成 4 列。
"""
import argparse, csv, json, os, re, sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')
csv.field_size_limit(10 * 1024 * 1024)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D_P, D_R, D_O = (os.path.join(ROOT, x) for x in ('out/payloads', 'out/raw', 'out'))
LEXF = os.path.join(ROOT, 'data', 'label_lexicon.json')
GOLD_SUB = os.path.join(ROOT, '原始资料', '金标准-细分-v1-LLM初稿.csv')
from jev_rules import split_segments, find_evidence, sentiment, FIELD_MAP, merge_label_rows

sys.path.insert(0, os.path.join(ROOT, 'tools'))
try:
    from raw_to_csv import DEFAULT_THRESHOLD as _TAU_DEFAULT
except Exception:
    _TAU_DEFAULT = 0.6


def parse_name(fn):
    """S001__B__A2.jsonl -> ('S001','B','A2'); 旧命名 S001.jsonl -> ('S001','','')"""
    if not fn.endswith('.jsonl'):
        return None
    parts = fn[:-6].split('__')
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 1:
        return parts[0], '', ''
    return None


def load_gold(lex):
    """金标准 -> {sample: [可接受idx集合,...]}; 一个金标准行 = 一个 unit"""
    byname = defaultdict(list)
    for L in lex['labels']:
        byname[(L['cat'], L['sub'], L['label'])].append(L['idx'])
    gold_units = defaultdict(list)
    orphan = amb = 0
    for r in csv.DictReader(open(GOLD_SUB, encoding='utf-8-sig')):
        v = (r['词库原始行号'] or '').strip()
        s = (r['样本号'] or '').strip()
        nums = [int(n) - 1 for n in re.findall(r'\d+', v)]
        if nums:
            u = set(nums)
        else:
            cand = byname.get((r['类别'], r['细分类别'], r['标签']), [])
            if not cand:
                orphan += 1
                continue
            u = set(cand)
            amb += 1
        gold_units[s].append(u)
    return gold_units, orphan, amb


def load_probs(scheme, mode):
    """解析 raw -> {sample: {idx: p}} + 每样本 meta"""
    probs = defaultdict(dict)
    meta_raw = {}
    if not os.path.isdir(D_R):
        return probs, meta_raw
    for fn in sorted(os.listdir(D_R)):
        nm = parse_name(fn)
        if not nm:
            continue
        s, sc, md = nm
        if scheme and sc != scheme:
            continue
        if mode and md != mode:
            continue
        for ln in open(os.path.join(D_R, fn), encoding='utf-8'):
            ln = ln.strip()
            if not ln:
                continue
            rec = json.loads(ln)
            if rec.get('http') != 200:
                continue
            smp = rec.get('sample') or s
            body = rec.get('body') or {}
            m = meta_raw.setdefault(smp, {'model': body.get('model', ''), 'in_tok': 0,
                                          'lat': [], 'api': rec.get('base', '')})
            m['in_tok'] += (body.get('usage') or {}).get('input_tokens', 0)
            m['lat'].append(rec.get('latency_ms', 0))
            for qid, ans in (body.get('answers') or {}).items():
                q = re.match(r'q(\d+)$', qid)
                if not q:
                    continue
                p = None
                if isinstance(ans, dict):
                    for k in ('noul', 'probability', 'prob'):
                        if k in ans:
                            p = ans[k]
                            break
                if isinstance(p, (int, float)):
                    probs[smp][int(q.group(1))] = float(p)
    return probs, meta_raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scheme', default='', help='方案名, 对应文件名 {sample}__{scheme}__{mode}.jsonl; 空=全部')
    ap.add_argument('--mode', default='', help='问法粒度 A1|A2; 空=全部(注意与 --scheme 是两个维度)')
    ap.add_argument('--tau', type=float, default=None,
                    help='命中阈值; 不填则读 tools/raw_to_csv.py 的 DEFAULT_THRESHOLD(%.2f)' % _TAU_DEFAULT)
    ap.add_argument('--tau-lo', type=float, default=None, help='灰区下限, 默认 tau*0.6')
    ap.add_argument('--tau-hi', type=float, default=None, help='灰区上限, 默认 tau')
    ap.add_argument('--api-tag', default='')
    a = ap.parse_args()

    tau = _TAU_DEFAULT if a.tau is None else a.tau
    tau_lo = round(tau * 0.6, 3) if a.tau_lo is None else a.tau_lo
    tau_hi = tau if a.tau_hi is None else a.tau_hi

    lex = json.load(open(LEXF, encoding='utf-8'))
    labels = {L['idx']: L for L in lex['labels']}
    qgroups = {G['idx']: G for G in merge_label_rows(lex['labels'])}

    tid = {}
    gold_units, gold_orphan, gold_amb = load_gold(lex)
    for u in gold_units.values():
        for one in u:
            pass
    for r in csv.DictReader(open(GOLD_SUB, encoding='utf-8-sig')):
        v = (r['词库原始行号'] or '').strip()
        nums = [int(n) - 1 for n in re.findall(r'\d+', v)]
        if len(nums) == 1 and r.get('标签ID'):
            tid[nums[0]] = r['标签ID']

    probs, meta_raw = load_probs(a.scheme, a.mode)
    if not probs:
        print('!! scheme=%s mode=%s 没有可用的 200 响应' % (a.scheme or '*', a.mode or '*'))
        print('   文件名形如 out/raw/S001__B__A2.jsonl; --scheme 填 B, --mode 填 A2')
        return 1
    if not gold_units:
        print('!! 金标准为空: %s' % GOLD_SUB)
        return 1

    tag = ((a.scheme or 'ALL') + '_' + (a.mode or 'ALL')) if (a.scheme or a.mode) else 'ALL'

    # ---------- 预测两表 ----------
    SUB_COLS = ['样本号', '平台', '词库原始行号', '标签ID', '类别', '细分类别', '标签', '评价/提及对象',
                '表达类型', '情绪', '证据字段', '原文证据', 'Jev概率', '命中阈值', '情绪来源', '模型版本', 'API来源']
    SUM_COLS = ['样本号', '平台', '帖子ID（文本）', 'CSV记录序号', '发布时间', '文本字符数', '入样原因',
                '部分标签', '标签证据数', '灰区待复核数', '截断标记', '原帖链接']
    f_sub = open(os.path.join(D_O, '打标结果细分-Jev_%s.csv' % tag), 'w', encoding='utf-8-sig', newline='')
    f_sum = open(os.path.join(D_O, '打标结果汇总-Jev_%s.csv' % tag), 'w', encoding='utf-8-sig', newline='')
    ws, wu = csv.writer(f_sub), csv.writer(f_sum)
    ws.writerow(SUB_COLS); wu.writerow(SUM_COLS)

    lat_all = []
    tok_by_sample = {}
    emo_src = defaultdict(int)
    gray_n = 0
    # 标签级统计: {(sample, idx)}
    pred_pairs = set()
    dim = defaultdict(lambda: [0, 0, 0])          # cat -> [TP, FP, FN]
    fp_list = defaultdict(int)
    fn_list = defaultdict(int)
    fn_not_asked = 0

    for s in sorted(probs):
        tp_txt = os.path.join(D_P, s + '.text.txt')
        mp = os.path.join(D_P, s + '.meta.json')
        if not (os.path.exists(tp_txt) and os.path.exists(mp)):
            print('!! 缺 %s.text.txt 或 .meta.json, 跳过' % s)
            continue
        txt = open(tp_txt, encoding='utf-8').read()
        meta = json.load(open(mp, encoding='utf-8'))
        segs = split_segments(txt)
        P = probs[s]
        hits = sorted([i for i, p in P.items() if p >= tau])
        gray = [i for i in P if tau_lo <= P[i] <= tau_hi and P[i] < tau]
        gray_n += len(gray)
        mr = meta_raw.get(s, {})
        api = a.api_tag or ('官方' if 'typesafe.ai' in str(mr.get('api', ''))
                            else '中转:' + re.sub(r'https?://', '', str(mr.get('api', '?'))))
        lat_all += mr.get('lat', [])
        tok_by_sample[s] = mr.get('in_tok', 0)

        for i in hits:
            L = qgroups.get(i) or labels.get(i)
            if not L:
                continue
            pred_pairs.add((s, i))
            segname, pos, kw = find_evidence(txt, segs, L['hits'])
            if pos is None:
                emo, esrc, ev_f, ev_t = '不适用', '未定位', '语义命中未定位', ''
            else:
                emo = sentiment(txt, pos, kw, L)
                esrc = '窗口定位'
                ev_f = FIELD_MAP.get(segname, '')
                ev_t = re.sub(r'\s+', ' ', txt[max(0, pos - 30): pos + len(kw) + 30]).strip()
            emo_src[esrc] += 1
            ws.writerow([s, meta.get('site_name', ''), i + 1, tid.get(i, ''), L['cat'], L['sub'], L['label'],
                         '未提取', '未提取', emo, ev_f, ev_t, '%.3f' % P[i], tau, esrc,
                         mr.get('model', ''), api])
        names = [ (qgroups.get(i) or labels[i])['label'] for i in hits if (i in qgroups or i in labels)]
        part = '、'.join(names[:8]) + ('；另%d项见明细' % (len(names) - 8) if len(names) > 8 else '')
        wu.writerow([s, meta.get('site_name', ''), meta.get('id', ''), meta.get('csv_row_no', ''),
                     meta.get('publish_time', ''), meta.get('char_count_raw', ''), meta.get('reason', ''),
                     part, len(hits), len(gray), '是' if meta.get('truncated') else '否', meta.get('url', '')])
    f_sub.close(); f_sum.close()

    # ---------- 标签级 TP/FP/FN ----------
    gold_pairs = set()
    for s, units in gold_units.items():
        for u in units:
            for i in u:
                gold_pairs.add((s, i))

    tp = len(pred_pairs & gold_pairs)
    fp = len(pred_pairs - gold_pairs)
    fn = len(gold_pairs - pred_pairs)

    for s, i in (pred_pairs - gold_pairs):
        L = labels.get(i)
        if not L:
            continue
        dim[L['cat']][1] += 1
        fp_list['%s / %s' % (L['cat'], L['label'])] += 1
    for s, i in (pred_pairs & gold_pairs):
        L = labels.get(i)
        if L:
            dim[L['cat']][0] += 1
    for s, i in sorted(gold_pairs - pred_pairs):
        L = labels.get(i)
        if not L:
            continue
        dim[L['cat']][2] += 1
        asked = i in probs.get(s, {})
        if not asked:
            fn_not_asked += 1
        tag_ = '' if asked else '  [未送问]'
        fn_list['%s / %s%s' % (L['cat'], L['label'], tag_)] += 1

    # unit 级召回(辅助参考): 一个金标准行里任一候选被命中即算覆盖
    covered_units = 0
    total_units = 0
    for s, units in gold_units.items():
        H = {i for i, p in probs.get(s, {}).items() if p >= tau}
        for u in units:
            total_units += 1
            if u & H:
                covered_units += 1

    # ---------- 报告 ----------
    pr = tp / (tp + fp) if tp + fp else 0
    rc = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0
    lat = sorted(lat_all)
    n_emo = sum(emo_src.values()) or 1
    rep = []
    rep.append('# Jev 方案④ 评测报告 (scheme=%s, mode=%s, tau=%.2f)\n' % (a.scheme or '*', a.mode or '*', tau))
    rep.append('> ⚠️ 对齐对象是 **LLM 初稿未经人审**(方案 §11-G7), 本报告数字只用于口径开发与冒烟, 不能写入 95% 结论。\n')
    rep.append('## 核心指标(标签级: 按 (样本号, 标签) 唯一对计)\n')
    rep.append('| 指标 | 值 |\n| --- | --- |')
    rep.append('| 样本数 | %d |' % len(probs))
    rep.append('| 金标准标签数 | %d |' % len(gold_pairs))
    rep.append('| 预测标签数 | %d |' % len(pred_pairs))
    rep.append('| 命中(TP) | %d |' % tp)
    rep.append('| 标错(FP) | %d |' % fp)
    rep.append('| 漏标(FN) | %d |' % fn)
    rep.append('| **准确率 P** | **%.1f%%** |' % (100 * pr))
    rep.append('| **召回率 R** | **%.1f%%** |' % (100 * rc))
    rep.append('| **F1** | **%.3f** |' % f1)
    rep.append('\n## 成本与稳定性\n')
    rep.append('| 指标 | 值 |\n| --- | --- |')
    rep.append('| 平均输入tok/帖 | %.0f |' % (sum(tok_by_sample.values()) / max(1, len(tok_by_sample))))
    rep.append('| 延迟 ms (p50/p95) | %s / %s |' % (
        lat[len(lat) // 2] if lat else '-', lat[min(len(lat) - 1, int(len(lat) * .95))] if lat else '-'))
    rep.append('| 情绪来源=窗口定位 覆盖率 | %.1f%% |' % (100 * emo_src.get('窗口定位', 0) / n_emo))
    rep.append('| 灰区(%.2f~%.2f)待复核 | %d |' % (tau_lo, tau_hi, gray_n))
    rep.append('\n## 口径与漏标结构\n')
    rep.append('| 指标 | 值 | 说明 |\n| --- | --- | --- |')
    rep.append('| 金标准行(unit)数 | %d | 同一标签在不同证据片段会拆成多行 |' % total_units)
    rep.append('| 去重后标签数 | %d | 本报告的 P/R/F1 分母 |' % len(gold_pairs))
    rep.append('| unit 覆盖率 | %.1f%% (%d/%d) | 辅助参考, 不与 P 组 F1 |' % (
        100 * covered_units / total_units if total_units else 0, covered_units, total_units))
    rep.append('| 其中 FN=未送问 | %d | 方案粗筛就没把该标签送进模型, 非模型判错 |' % fn_not_asked)
    rep.append('| 金标准行靠重名展开 | %d | 无行号, 按三元组匹配 |' % gold_amb)
    rep.append('| 金标准孤儿行 | %d | 三元组也查不到, 已剔除 |' % gold_orphan)
    if fn:
        rep.append('\n> FN 中有 **%d/%d (%.0f%%)** 是"粗筛未送问"造成的结构性漏召,'
                   ' 这部分不是 Jev 判错, 是方案前置筛选的天花板。' % (fn_not_asked, fn, 100 * fn_not_asked / fn))
    rep.append('\n## 分维度 P/R/F1\n')
    rep.append('| 维度 | TP | FP | FN | P | R | F1 |')
    rep.append('| --- | --- | --- | --- | --- | --- | --- |')
    for c, (t, f_, n_) in sorted(dim.items(), key=lambda kv: -sum(kv[1])):
        p_ = t / (t + f_) if t + f_ else 0
        r_ = t / (t + n_) if t + n_ else 0
        F_ = 2 * p_ * r_ / (p_ + r_) if p_ + r_ else 0
        rep.append('| %s | %d | %d | %d | %.2f | %.2f | %.2f |' % (c, t, f_, n_, p_, r_, F_))

    def tops(d, title, n=15):
        rep.append('\n## %s (TOP%d)\n' % (title, n))
        rep.append('| 维度 · 标签 | 次数 |')
        rep.append('| --- | --- |')
        for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]:
            rep.append('| %s | %d |' % (k, v))

    tops(fp_list, '疑似误报(FP)')
    tops(fn_list, '疑似漏报(FN)')

    out = os.path.join(D_O, '评测报告_%s.md' % tag)
    open(out, 'w', encoding='utf-8').write('\n'.join(rep) + '\n')
    print('细分/汇总/报告已输出:')
    print(' ', os.path.join(D_O, '打标结果细分-Jev_%s.csv' % tag))
    print(' ', os.path.join(D_O, '打标结果汇总-Jev_%s.csv' % tag))
    print(' ', out)
    print('准确率P=%.1f%%  召回率R=%.1f%%  F1=%.3f   (TP=%d FP=%d FN=%d, tau=%.2f)'
          % (100 * pr, 100 * rc, f1, tp, fp, fn, tau))
    print('其中 FN 未送问 %d 个 (%.0f%%); unit 覆盖率 %.1f%%'
          % (fn_not_asked, 100 * fn_not_asked / fn if fn else 0,
             100 * covered_units / total_units if total_units else 0))


if __name__ == '__main__':
    sys.exit(main() or 0)
