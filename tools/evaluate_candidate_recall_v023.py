# -*- coding: utf-8 -*-
r"""HybridRAG 候选召回评测：帖子 ID 对齐，标签文本逐帖去重。

只评估候选层，不调用 Jev：
  python tools\evaluate_candidate_recall_v023.py `
    --candidates out\payloads_HybridRAG\候选标签_HybridRAG.csv `
    --top-k 20,50,100,200
"""
import argparse
import csv
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from compare_labels_v023 import (DEFAULT_GOLD_OV, DEFAULT_GOLD_EV, norm_text,
                                 read_table, unique_index, pct)


def _rank(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def evaluate(candidates, gold_overview, gold_evidence, outdir, top_ks, tag='', min_score=None):
    rows = read_table(candidates, {'样本号', '帖子ID', '标签', '正则命中', 'RAG排名'})
    gold_ov = read_table(gold_overview, {'样本号', '帖子ID（文本）'})
    gold_ev = read_table(gold_evidence, {'样本号', '标签'})
    gold_id_to_p = unique_index(gold_ov, '帖子ID（文本）', '标准答案总览')
    unique_index(gold_ov, '样本号', '标准答案总览')

    # 候选文件定义实验范围；空标签行是 0 候选帖子的占位，不作为标签。
    scope = {}
    by_post = defaultdict(list)
    anomalies = []
    for row in rows:
        post_id = norm_text(row.get('帖子ID'))
        sample = norm_text(row.get('样本号'))
        if not post_id or not sample:
            anomalies.append((sample, post_id, '候选行缺少帖子ID或样本号'))
            continue
        if post_id in scope and scope[post_id] != sample:
            anomalies.append((sample, post_id, '同一帖子ID对应多个预测样本号'))
            continue
        scope[post_id] = sample
        by_post[post_id].append(row)

    gold_by_p = defaultdict(set)
    for row in gold_ev:
        label = norm_text(row.get('标签'))
        if label:
            gold_by_p[norm_text(row.get('样本号'))].add(label)

    valid = []
    for post_id, sample in sorted(scope.items()):
        gold_row = gold_id_to_p.get(post_id)
        if not gold_row:
            anomalies.append((sample, post_id, '标准答案找不到帖子ID'))
            continue
        p_id = norm_text(gold_row.get('样本号'))
        valid.append((post_id, sample, gold_by_p.get(p_id, set())))

    summaries, missed = [], []
    for k in top_ks:
        totals = defaultdict(int)
        for post_id, sample, gold in valid:
            post_rows = by_post[post_id]
            direct = {norm_text(r.get('标签')) for r in post_rows
                      if norm_text(r.get('标签')) and norm_text(r.get('是否直接打标')).upper() == 'Y'}
            regex = {norm_text(r.get('标签')) for r in post_rows
                     if norm_text(r.get('标签')) and norm_text(r.get('正则命中')).upper() == 'Y'}
            rag = {norm_text(r.get('标签')) for r in post_rows
                   if norm_text(r.get('标签')) and (_rank(r.get('RAG排名')) or 10 ** 9) <= k
                   and (min_score is None or (norm_text(r.get('RAG相似度')) and float(r.get('RAG相似度')) >= min_score))}
            union = regex | rag
            totals['gold'] += len(gold)
            totals['direct'] += len(direct)
            totals['direct_hit'] += len(gold & direct)
            totals['jev_questions'] += len((regex | rag) - direct)
            totals['regex_hit'] += len(gold & regex)
            totals['rag_hit'] += len(gold & rag)
            totals['union_hit'] += len(gold & union)
            totals['candidates'] += len(union)
            totals['regex_only'] += len(gold & regex - rag)
            totals['rag_only'] += len(gold & rag - regex)
            totals['both'] += len(gold & regex & rag)
            for label in sorted(gold - union):
                missed.append({'TopK': k, '帖子ID': post_id, '样本号': sample, '标签': label,
                               '原因': '正则未命中且未进入RAG Top-%d' % k})

        denom = totals['gold']
        n_posts = len(valid)
        summary = {
            'TopK': k, '评测帖子数': n_posts, '标准答案标签总数': denom,
            '强正则直接标签数': totals['direct'], '强正则直接命中数': totals['direct_hit'],
            '强正则直接准确率': totals['direct_hit'] / totals['direct'] if totals['direct'] else 0,
            '强正则直接召回率': totals['direct_hit'] / denom if denom else 0,
            '正则候选召回数': totals['regex_hit'],
            '正则候选召回率': totals['regex_hit'] / denom if denom else 0,
            'RAG候选召回数': totals['rag_hit'],
            'RAG候选召回率': totals['rag_hit'] / denom if denom else 0,
            '并集候选召回数': totals['union_hit'],
            '并集候选召回率': totals['union_hit'] / denom if denom else 0,
            '并集漏标数': denom - totals['union_hit'],
            '并集候选标签数': totals['candidates'],
            '平均每帖候选数': totals['candidates'] / n_posts if n_posts else 0,
            '平均每帖Jev问数': totals['jev_questions'] / n_posts if n_posts else 0,
            '仅正则补回': totals['regex_only'], '仅RAG补回': totals['rag_only'],
            '两路共同覆盖': totals['both'],
        }
        summaries.append(summary)

    os.makedirs(outdir, exist_ok=True)
    suffix = ('_' + tag) if tag else ''
    summary_path = os.path.join(outdir, '候选召回汇总_v0.2.3%s.csv' % suffix)
    missed_path = os.path.join(outdir, '候选漏标明细_v0.2.3%s.csv' % suffix)
    report_path = os.path.join(outdir, '候选召回报告_v0.2.3%s.md' % suffix)

    fields = list(summaries[0].keys()) if summaries else ['TopK']
    with open(summary_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in summaries:
            out = dict(row)
            for key in ('正则候选召回率', 'RAG候选召回率', '并集候选召回率'):
                out[key] = '%.6f' % row[key]
            out['平均每帖候选数'] = '%.2f' % row['平均每帖候选数']
            out['平均每帖Jev问数'] = '%.2f' % row['平均每帖Jev问数']
            for key in ('强正则直接准确率', '强正则直接召回率'):
                out[key] = '%.6f' % row[key]
            writer.writerow(out)
    with open(missed_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['TopK', '帖子ID', '样本号', '标签', '原因'])
        writer.writeheader(); writer.writerows(missed)

    report = ['# HybridRAG 候选召回报告', '',
              '> 仅评估候选层，不代表最终 Jev 打标效果。按帖子 ID 对齐，同帖同名标签去重。', '',
              '| TopK | 帖子数 | 标准标签数 | 正则召回 | RAG召回 | 并集召回 | 并集漏标 | 平均候选/帖 | 仅正则补回 | 仅RAG补回 | 共同覆盖 |',
              '| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    if summaries:
        first = summaries[0]
        report += ['', '## 强正则直接打标（不经过 Jev，与 TopK 无关）', '',
                   '| 直接标签数 | 其中标准答案正确 | 直接准确率 | 直接召回率 |',
                   '| ---: | ---: | ---: | ---: |',
                   '| %d | %d | **%s** | **%s** |' % (
                       first['强正则直接标签数'], first['强正则直接命中数'],
                       pct(first['强正则直接准确率']), pct(first['强正则直接召回率']))]
    for r in summaries:
        report.append('| %d | %d | %d | %s | %s | **%s** | %d | %.2f | %d | %d | %d |' % (
            r['TopK'], r['评测帖子数'], r['标准答案标签总数'], pct(r['正则候选召回率']),
            pct(r['RAG候选召回率']), pct(r['并集候选召回率']), r['并集漏标数'],
            r['平均每帖候选数'], r['仅正则补回'], r['仅RAG补回'], r['两路共同覆盖']))
    report += ['', '## 口径说明', '',
               '- 强正则直接标签：标签名精确出现且附近无否定，概率按本地 1.0 写入最终结果，不送 Jev。',
               '- 正则候选：标签名精确出现的 direct/review 标签；review 是否定命中，仍需 Jev 复核。',
               '- RAG候选：该帖“RAG排名 ≤ TopK”的标签。',
               '- 并集候选：强正则直接标签、否定复核标签与 RAG 候选按标签文本合并去重。',
               '- 平均每帖 Jev 问数 =（否定复核 ∪ RAG TopK）－强正则直接标签。',
               '- 候选召回率 = 进入候选集合的标准答案标签数 ÷ 标准答案标签总数。',
               '- 最低余弦相似度过滤：%s。' % ('未启用' if min_score is None else min_score),
               '- “仅正则补回/仅RAG补回”均只统计标准答案中的正确标签，不是全部候选数量。',
               '- 异常帖子不进入指标，禁止按行号回退。', '',
               '评测范围帖子：%d；成功纳入：%d；异常：%d。' % (len(scope), len(valid), len(anomalies)), '',
               '漏标明细逐 TopK 输出；同一正确标签在不同 TopK 下可能重复出现。']
    if anomalies:
        report += ['', '## 异常示例', '']
        report += ['- %s / %s：%s' % row for row in anomalies[:30]]
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report) + '\n')

    return {'summary': summary_path, 'missed': missed_path, 'report': report_path,
            'rows': summaries, 'posts': len(valid), 'anomalies': len(anomalies)}


def main():
    ap = argparse.ArgumentParser('HybridRAG 候选召回评测（不调用 Jev）')
    ap.add_argument('--candidates', required=True, help='schemes.py 生成的 候选标签_HybridRAG.csv')
    ap.add_argument('--gold-overview', default=DEFAULT_GOLD_OV)
    ap.add_argument('--gold-evidence', default=DEFAULT_GOLD_EV)
    ap.add_argument('--top-k', default='20,50,100,200', help='逗号分隔，默认 20,50,100,200')
    ap.add_argument('--outdir', default='', help='默认候选 CSV 所在目录')
    ap.add_argument('--tag', default='', help='输出文件名后缀')
    ap.add_argument('--min-score', type=float, default=None, help='与生成请求体一致的最低RAG余弦相似度；默认不过滤')
    args = ap.parse_args()
    try:
        top_ks = sorted({int(x.strip()) for x in args.top_k.split(',') if x.strip()})
        if not top_ks or any(k <= 0 for k in top_ks):
            raise ValueError('--top-k 必须是正整数列表')
        result = evaluate(os.path.abspath(args.candidates), os.path.abspath(args.gold_overview),
                          os.path.abspath(args.gold_evidence),
                          os.path.abspath(args.outdir) if args.outdir else os.path.dirname(os.path.abspath(args.candidates)),
                          top_ks, args.tag, args.min_score)
    except (ValueError, OSError) as exc:
        print('!! %s' % exc)
        return 2
    print('候选召回评测完成：%d 帖，异常 %d' % (result['posts'], result['anomalies']))
    for row in result['rows']:
        print('Top-%-3d 正则=%s RAG=%s 并集=%s 平均候选=%.2f' %
              (row['TopK'], pct(row['正则候选召回率']), pct(row['RAG候选召回率']),
               pct(row['并集候选召回率']), row['平均每帖候选数']))
    print('报告: %s' % result['report'])
    print('汇总: %s' % result['summary'])
    print('漏标: %s' % result['missed'])
    return 0 if result['posts'] else 1


if __name__ == '__main__':
    sys.exit(main())
