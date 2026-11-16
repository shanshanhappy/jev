# -*- coding: utf-8 -*-
r"""jev_eval.py  第5步: 原始响应 + 词库 + 金标准 -> 预测两表 + 评测报告
职责(见 docs\数据处理方案.md §10.2~10.5):
  1) 解析 out\raw\{sample}__{mode}.jsonl 的 Noul 概率 -> 按阈值判命中(这是 Jev 唯一的输出)
  2) 词库反查标签四件套; 证据回搜(命中关键词->所在【来源段】与原文窗口); 情绪 near 窗口规则
  3) 输出预测 细分17列 / 汇总12列, 并与金标准按 (样本号, 词库原始行号) 双键对分

用法:
  python core\jev_eval.py --mode A2 --tau 0.5
  python tools\jev_eval.py --mode B  --tau 0.5 --tau-lo 0.3 --tau-hi 0.7
输出:
  out\打标结果细分-Jev_{mode}.csv   out\打标结果汇总-Jev_{mode}.csv   out\评测报告_{mode}.md
"""
import argparse, csv, json, os, re, sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')
csv.field_size_limit(10 * 1024 * 1024)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D_P, D_R, D_O = (os.path.join(ROOT, x) for x in ('out/payloads', 'out/raw', 'out'))
LEXF = os.path.join(ROOT, 'data', 'label_lexicon.json')
GOLD_SUB = os.path.join(ROOT, '原始资料', '金标准-细分-v1-LLM初稿.csv')
GOLD_SUM = os.path.join(ROOT, '原始资料', '金标准-汇总-v1-LLM初稿.csv')
from jev_rules import split_segments, find_evidence, sentiment, FIELD_MAP, merge_label_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', default='A2', help='A1|A2|B (与 payload 一致)')
    ap.add_argument('--tau', type=float, default=0.5)
    ap.add_argument('--tau-lo', type=float, default=0.3)
    ap.add_argument('--tau-hi', type=float, default=0.7)
    ap.add_argument('--api-tag', default='')
    a = ap.parse_args()

    lex = json.load(open(LEXF, encoding='utf-8'))
    labels = {L['idx']: L for L in lex['labels']}       # 原始行：金标准映射用
    qgroups = {G['idx']: G for G in merge_label_rows(lex['labels'])}  # 聚合标签：预测输出/证据回搜用

    tid = {}                                     # idx -> 标签ID(从金标准反推, 缺则留空)
    byname = defaultdict(list)                   # (cat,sub,label) -> [idx...] 词库重名多行
    for L in lex['labels']:
        byname[(L['cat'], L['sub'], L['label'])].append(L['idx'])
    gold_units = defaultdict(list)               # sample -> [可接受idx集合,...] 一个金标准标签=一个unit
    gold_orphan = 0                              # 连三元组都查不到的行(应=0)
    gold_amb = 0                                 # 行号缺失、靠重名多行展开的 unit 数
    for r in csv.DictReader(open(GOLD_SUB, encoding='utf-8-sig')):
        v = (r['词库原始行号'] or '').strip()
        nums = [int(n) - 1 for n in re.findall(r'\d+', v)]   # 兼容 "533、534…" 列表形态(色号型)
        if nums:
            u = set(nums)                        # 金标准行号 = 数据行号+1 (方案 §2.4)
        else:
            cand = byname.get((r['类别'], r['细分类别'], r['标签']), [])
            if not cand:
                gold_orphan += 1
                continue
            u = set(cand)                        # 重名多行: 任一候选命中即算命中
            gold_amb += 1
        gold_units[r['样本号']].append(u)
        if len(u) == 1 and r['标签ID']:
            tid[next(iter(u))] = r['标签ID']
    # (旧版从金标准汇总取入样原因; v1.1 起该元数据由 jev_prompts 写进 meta.json, 抽样清单=生产唯一来源)

    probs = defaultdict(dict)                    # sample -> {idx: p}
    meta_raw = {}                                # sample -> {model_version, in_tok, lat}
    if not os.path.isdir(D_R):
        print('!! 没有 out/raw, 先跑 jev_client.py'); return 1
    for fn in os.listdir(D_R):
        if not fn.endswith('.jsonl') or ('__' + a.mode + '.') not in fn:
            continue
        for ln in open(os.path.join(D_R, fn), encoding='utf-8'):
            rec = json.loads(ln)
            if rec.get('http') != 200:
                continue
            s = rec['sample']
            body = rec.get('body') or {}
            m = meta_raw.setdefault(s, {'model': body.get('model', ''), 'in_tok': 0, 'lat': [], 'api': rec.get('base', '')})
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
                            p = ans[k]; break
                if isinstance(p, (int, float)):
                    probs[s][int(q.group(1))] = float(p)
    if not probs:
        print('!! mode=%s 没有可用的 200 响应' % a.mode); return 1

    # ---------- 预测两表 ----------
    SUB_COLS = ['样本号', '平台', '词库原始行号', '标签ID', '类别', '细分类别', '标签', '评价/提及对象',
                '表达类型', '情绪', '证据字段', '原文证据', 'Jev概率', '命中阈值', '情绪来源', '模型版本', 'API来源']
    SUM_COLS = ['样本号', '平台', '帖子ID（文本）', 'CSV记录序号', '发布时间', '文本字符数', '入样原因',
                '部分标签', '标签证据数', '灰区待复核数', '截断标记', '原帖链接']
    f_sub = open(os.path.join(D_O, '打标结果细分-Jev_%s.csv' % a.mode), 'w', encoding='utf-8-sig', newline='')
    f_sum = open(os.path.join(D_O, '打标结果汇总-Jev_%s.csv' % a.mode), 'w', encoding='utf-8-sig', newline='')
    ws, wu = csv.writer(f_sub), csv.writer(f_sum)
    ws.writerow(SUB_COLS); wu.writerow(SUM_COLS)

    stats = {'tp': 0, 'fp': 0, 'fn': 0, 'dim': defaultdict(lambda: [0, 0, 0]),
             'emo': defaultdict(int), 'fp_list': defaultdict(int), 'fn_list': defaultdict(int)}
    lat_all, tok_by_sample = [], {}

    for s in sorted(probs):
        txt = open(os.path.join(D_P, s + '.text.txt'), encoding='utf-8').read()
        meta = json.load(open(os.path.join(D_P, s + '.meta.json'), encoding='utf-8'))
        segs = split_segments(txt)
        P = probs[s]
        hits = sorted([i for i, p in P.items() if p >= a.tau])
        gray = [i for i, p in P.items() if a.tau_lo <= p <= a.tau_hi]
        mr = meta_raw.get(s, {})
        api = a.api_tag or ('官方' if 'typesafe.ai' in str(mr.get('api', '')) else '中转:' + re.sub(r'https?://', '', str(mr.get('api', '?'))))
        lat_all += mr.get('lat', []); tok_by_sample[s] = mr.get('in_tok', 0)

        for i in hits:
            L = qgroups.get(i) or labels.get(i)
            if not L:
                continue
            segname, pos, kw = find_evidence(txt, segs, L['hits'])
            if pos is None:
                emo, esrc, ev_f, ev_t = '不适用', '未定位', '语义命中未定位', ''
            else:
                emo = sentiment(txt, pos, kw, L)
                esrc = '窗口定位'
                ev_f = FIELD_MAP[segname]
                ev_t = re.sub(r'\s+', ' ', txt[max(0, pos - 30): pos + len(kw) + 30]).strip()
            stats['emo'][esrc] += 1
            ws.writerow([s, meta['site_name'], i + 1, tid.get(i, ''), L['cat'], L['sub'], L['label'],
                         '未提取', '未提取', emo, ev_f, ev_t, '%.3f' % P[i], a.tau, esrc, mr.get('model', ''), api])
        names = [labels[i]['label'] for i in hits if i in labels]
        part = '、'.join(names[:8]) + ('；另%d项见明细' % (len(names) - 8) if len(names) > 8 else '')
        wu.writerow([s, meta['site_name'], meta['id'], meta['csv_row_no'], meta['publish_time'],
                     meta['char_count_raw'], meta.get('reason', ''), part, len(hits), len(gray),
                     '是' if meta['truncated'] else '否', meta['url']])
        H = set(hits)
        units = gold_units.get(s, [])
        covered = []
        for u in units:
            if u & H:
                covered.append(u)
                rep = next(iter(u))
                stats['tp'] += 1; stats['dim'][labels[rep]['cat']][0] += 1
            else:
                rep = next(iter(u))
                stats['fn'] += 1; stats['dim'][labels[rep]['cat']][2] += 1
                tag_ = '(重名未中)' if len(u) > 1 else ('(未送问)' if a.mode == 'B' and rep not in P else '')
                stats['fn_list']['%s|%s%s' % (labels[rep]['cat'], labels[rep]['label'], tag_)] += 1
        for i in H:
            if not any(i in u for u in units):
                stats['fp'] += 1; stats['dim'][labels[i]['cat']][1] += 1
                stats['fp_list']['%s|%s' % (labels[i]['cat'], labels[i]['label'])] += 1
    f_sub.close(); f_sum.close()

    # ---------- 报告 ----------
    tp, fp, fn = stats['tp'], stats['fp'], stats['fn']
    pr = tp / (tp + fp) if tp + fp else 0; rc = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0
    lat = sorted(lat_all)
    n_emo = sum(stats['emo'].values()) or 1
    rep = []
    rep.append('# Jev 方案④ 评测报告 (mode=%s, tau=%.2f)\n' % (a.mode, a.tau))
    rep.append('> ⚠️ 对齐对象是 **LLM 初稿未经人审**(方案 §11-G7), 本报告数字只用于口径开发与冒烟, 不能写入 95% 结论。\n')
    rep.append('| 指标 | 值 |\n| --- | --- |')
    rep.append('| 样本数 | %d |' % len(probs))
    rep.append('| Micro P / R / F1 | %.3f / %.3f / %.3f |' % (pr, rc, f1))
    rep.append('| 金标准 unit 数 | %d（其中重名多行展开 %d，孤儿剔除 %d）|' % (sum(len(v) for v in gold_units.values()), gold_amb, gold_orphan))
    rep.append('| TP/FP/FN | %d / %d / %d |' % (tp, fp, fn))
    rep.append('| 平均输入tok/帖 | %.0f |' % (sum(tok_by_sample.values()) / max(1, len(tok_by_sample))))
    rep.append('| 延迟 ms (p50/p95) | %s / %s |' % (lat[len(lat)//2] if lat else '-', lat[int(len(lat)*.95)-1] if lat else '-'))
    rep.append('| 情绪来源=窗口定位 覆盖率 | %.1f%% |' % (100 * stats['emo'].get('窗口定位', 0) / n_emo))
    rep.append('\n## 分维度 P/R/F1\n\n| 维度 | TP | FP | FN | P | R | F1 |')
    rep.append('| --- | --- | --- | --- | --- | --- | --- |')
    for c, (t, f_, n_) in sorted(stats['dim'].items(), key=lambda kv: -sum(kv[1])):
        p_ = t / (t + f_) if t + f_ else 0; r_ = t / (t + n_) if t + n_ else 0
        F_ = 2 * p_ * r_ / (p_ + r_) if p_ + r_ else 0
        rep.append('| %s | %d | %d | %d | %.2f | %.2f | %.2f |' % (c, t, f_, n_, p_, r_, F_))
    def tops(d, title, n=15):
        rep.append('\n## %s (TOP%d)\n\n| 维度|标签 | 次数 |\n| --- | --- |' % (title, n))
        for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]:
            rep.append('| %s | %d |' % (k, v))
    tops(stats['fp_list'], '疑似误报(FP)'); tops(stats['fn_list'], '疑似漏报(FN)')
    out = os.path.join(D_O, '评测报告_%s.md' % a.mode)
    open(out, 'w', encoding='utf-8').write('\n'.join(rep) + '\n')
    print('细分/汇总/报告已输出:'); print(' ', os.path.join(D_O, '打标结果细分-Jev_%s.csv' % a.mode))
    print(' ', os.path.join(D_O, '打标结果汇总-Jev_%s.csv' % a.mode)); print(' ', out)
    print('Micro P/R/F1 = %.3f / %.3f / %.3f  (tp=%d fp=%d fn=%d)' % (pr, rc, f1, tp, fp, fn))


if __name__ == '__main__':
    sys.exit(main() or 0)