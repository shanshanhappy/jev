# -*- coding: utf-8 -*-
r"""compare_labels_v023.py

以帖子 ID 对齐预测与 v0.2.3 标准答案，只按“标签”列文本比较。

示例：
  python tools\compare_labels_v023.py ^
    --pred-detail out\raw_Speculative_1000\打标结果细分-Jev_xxx.csv ^
    --pred-summary out\raw_Speculative_1000\打标结果汇总-Jev_xxx.csv

输出：标签对比明细、逐帖汇总、异常清单、Markdown 报告。
"""
import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
csv.field_size_limit(10 ** 9)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GOLD_OV = os.path.join(ROOT, 'data', '新品雷达_正式标注数据_v0.2.3_全量打标总览.csv')
DEFAULT_GOLD_EV = os.path.join(ROOT, 'data', '新品雷达_正式标注数据_v0.2.3_标签与证据.csv')


def norm_text(v):
    """只归一空白，不做同义词、类别或词库行号归并。"""
    return ' '.join(str(v or '').split()).strip()


def read_table(path, required):
    """自动定位表头：前 10 行内首个包含全部 required 列名的行。"""
    if not os.path.exists(path):
        raise ValueError('文件不存在: %s' % path)
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    for i, row in enumerate(rows[:10]):
        header = [norm_text(x) for x in row]
        if set(required) <= set(header):
            out = []
            for raw in rows[i + 1:]:
                if not any(norm_text(x) for x in raw):
                    continue
                values = raw + [''] * max(0, len(header) - len(raw))
                out.append({header[j]: values[j] if j < len(values) else '' for j in range(len(header))})
            return out
    raise ValueError('前10行找不到表头，需要列 %s：%s' % (required, path))


def unique_index(rows, key, table_name):
    """建立唯一键索引；空键或重复键直接拒绝，避免静默对错帖子。"""
    index = {}
    duplicates = set()
    for r in rows:
        value = norm_text(r.get(key))
        if not value:
            raise ValueError('%s 存在空 %s' % (table_name, key))
        if value in index:
            duplicates.add(value)
        else:
            index[value] = r
    if duplicates:
        raise ValueError('%s 的 %s 不唯一，示例：%s' % (table_name, key, sorted(duplicates)[:5]))
    return index


def pct(v):
    return '%.1f%%' % (100 * v)


def metrics(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f1


def meta_by_label(rows, sample_key, sample_value):
    """{标签: [(类别, 细分类别), ...]}，标签与类别均去重，证据行不参与计数。"""
    out = defaultdict(set)
    for r in rows:
        if norm_text(r.get(sample_key)) != sample_value:
            continue
        label = norm_text(r.get('标签'))
        if label:
            out[label].add((norm_text(r.get('类别')), norm_text(r.get('细分类别'))))
    return out


def choose_meta(pred_meta, gold_meta):
    """标签名相同后，优先选择类别+细分类别也一致的一对，便于诊断。"""
    if not pred_meta:
        return ('', ''), ('', ''), '', ''
    if not gold_meta:
        return next(iter(pred_meta)), ('', ''), '', ''
    for p in sorted(pred_meta):
        for g in sorted(gold_meta):
            if p == g:
                return p, g, '是', '是'
    for p in sorted(pred_meta):
        for g in sorted(gold_meta):
            if p[0] == g[0]:
                return p, g, '是', '否'
    return next(iter(pred_meta)), next(iter(gold_meta)), '否', '否'


def auto_tag(pred_rows):
    taus = sorted({norm_text(r.get('命中阈值')) for r in pred_rows if norm_text(r.get('命中阈值'))})
    if len(taus) == 1:
        return 'tau_' + taus[0].replace('.', '_')
    return ''


def compare(pred_detail, pred_summary, gold_overview, gold_evidence, outdir, tag=''):
    pred_sum_rows = read_table(pred_summary, {'样本号', '帖子ID（文本）'})
    pred_rows = read_table(pred_detail, {'样本号', '标签'})
    gold_ov_rows = read_table(gold_overview, {'样本号', '帖子ID（文本）'})
    gold_ev_rows = read_table(gold_evidence, {'样本号', '标签'})
    if not tag:
        tag = auto_tag(pred_rows)

    # 标准答案侧主键必须唯一；否则任何行号回退都可能对错帖子。
    gold_id_to_p = unique_index(gold_ov_rows, '帖子ID（文本）', '标准答案总览')
    gold_p_to_id = unique_index(gold_ov_rows, '样本号', '标准答案总览')
    del gold_p_to_id  # 只做唯一性校验

    # 预测汇总侧的 S 号和帖子 ID 同样要求唯一。
    pred_s_to_row = unique_index(pred_sum_rows, '样本号', '预测汇总')
    pred_id_to_row = unique_index(pred_sum_rows, '帖子ID（文本）', '预测汇总')
    del pred_id_to_row

    pred_detail_samples = defaultdict(list)
    for r in pred_rows:
        pred_detail_samples[norm_text(r.get('样本号'))].append(r)

    gold_by_p = defaultdict(list)
    for r in gold_ev_rows:
        gold_by_p[norm_text(r.get('样本号'))].append(r)

    details, anomalies, sample_rows = [], [], []
    seen_pred_samples = set()

    def anomaly(sample, post_id, kind, note):
        anomalies.append({'预测样本号': sample, '帖子ID': post_id, '异常类型': kind, '说明': note})

    # 预测汇总定义评测范围，确保“0 个预测标签”的帖子仍进入 FN 统计。
    for s in sorted(pred_s_to_row):
        row = pred_s_to_row[s]
        post_id = norm_text(row.get('帖子ID（文本）'))
        seen_pred_samples.add(s)
        gold_ov = gold_id_to_p.get(post_id)
        if not gold_ov:
            anomaly(s, post_id, '标准答案找不到帖子ID', '该样本不参与指标计算，禁止按行号回退')
            continue
        p_id = norm_text(gold_ov.get('样本号'))
        pred_meta = meta_by_label(pred_detail_samples.get(s, []), '样本号', s)
        gold_meta = meta_by_label(gold_by_p.get(p_id, []), '样本号', p_id)
        pred_labels, gold_labels = set(pred_meta), set(gold_meta)
        tp_set, fp_set, fn_set = pred_labels & gold_labels, pred_labels - gold_labels, gold_labels - pred_labels

        for label in sorted(tp_set):
            details.append([post_id, s, p_id, label, 'TP'])
        for label in sorted(fp_set):
            details.append([post_id, s, p_id, label, 'FP'])
        for label in sorted(fn_set):
            details.append([post_id, s, p_id, label, 'FN'])

        tp, fp, fn = len(tp_set), len(fp_set), len(fn_set)
        precision, recall, f1 = metrics(tp, fp, fn)
        sample_rows.append([post_id, s, p_id, len(pred_labels), len(gold_labels), tp, fp, fn,
                            '%.6f' % precision, '%.6f' % recall, '%.6f' % f1])

    for s in sorted(set(pred_detail_samples) - seen_pred_samples):
        anomaly(s, '', '预测细分样本不在汇总表', '无法取得帖子ID，该样本不参与指标计算')

    tp = sum(r[5] for r in sample_rows)
    fp = sum(r[6] for r in sample_rows)
    fn = sum(r[7] for r in sample_rows)
    precision, recall, f1 = metrics(tp, fp, fn)

    os.makedirs(outdir, exist_ok=True)
    suffix = ('_' + tag) if tag else ''
    detail_path = os.path.join(outdir, '标签对比明细_v0.2.3%s.csv' % suffix)
    sample_path = os.path.join(outdir, '标签对比样本汇总_v0.2.3%s.csv' % suffix)
    anomaly_path = os.path.join(outdir, '标签对比异常_v0.2.3%s.csv' % suffix)
    report_path = os.path.join(outdir, '标签对比报告_v0.2.3%s.md' % suffix)

    with open(detail_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['帖子ID', '预测样本号', '标准答案样本号', '标签', '对比结果'])
        w.writerows(details)
    with open(sample_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['帖子ID', '预测样本号', '标准答案样本号', '预测标签数', '标准标签数', 'TP', 'FP', 'FN', 'P', 'R', 'F1'])
        w.writerows(sample_rows)
    with open(anomaly_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['预测样本号', '帖子ID', '异常类型', '说明'])
        w.writeheader(); w.writerows(anomalies)

    fp_top = Counter(r[3] for r in details if r[4] == 'FP').most_common(15)
    fn_top = Counter(r[3] for r in details if r[4] == 'FN').most_common(15)

    rep = [
        '# v0.2.3 标签文本对比报告', '',
        '> 对齐方式：帖子 ID；标签口径：同一帖子内“标签”列去重后做精确文本集合比较。', '',
        '## 评测范围', '',
        '| 项目 | 值 |', '| --- | ---: |',
        '| 预测汇总样本数 | %d |' % len(pred_s_to_row),
        '| 成功匹配并纳入指标 | %d |' % len(sample_rows),
        '| 异常样本数 | %d |' % len(anomalies),
        '| 标签对比明细行数 | %d |' % len(details), '',
        '## 核心指标（micro）', '',
        '| 指标 | 值 |', '| --- | ---: |',
        '| 标准答案标签数（每帖去重后合计） | %d |' % (tp + fn),
        '| 预测标签数（每帖去重后合计） | %d |' % (tp + fp),
        '| TP | %d |' % tp, '| FP | %d |' % fp, '| FN | %d |' % fn,
        '| 准确率 P | %s |' % pct(precision), '| 召回率 R | %s |' % pct(recall),
        '| F1 | %.3f |' % f1, '',
        '## 高频多标 FP（TOP15）', '',
        '| 标签 | 次数 |', '| --- | ---: |',
    ]
    rep += ['| %s | %d |' % x for x in fp_top] or ['| 无 | 0 |']
    rep += ['', '## 高频漏标 FN（TOP15）', '', '| 标签 | 次数 |', '| --- | ---: |']
    rep += ['| %s | %d |' % x for x in fn_top] or ['| 无 | 0 |']
    rep += ['', '## 文件', '',
            '- 对比明细：`%s`' % os.path.basename(detail_path),
            '- 逐帖汇总：`%s`' % os.path.basename(sample_path),
            '- 异常清单：`%s`' % os.path.basename(anomaly_path), '',
            '生成时间：%s' % datetime.now().isoformat(timespec='seconds')]
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(rep) + '\n')

    return {
        'detail': detail_path, 'sample': sample_path, 'anomaly': anomaly_path, 'report': report_path,
        'samples': len(sample_rows), 'anomalies': len(anomalies), 'tp': tp, 'fp': fp, 'fn': fn,
        'p': precision, 'r': recall, 'f1': f1,
    }


def main():
    ap = argparse.ArgumentParser('以帖子ID对齐 v0.2.3 标准答案，只比较标签文本')
    ap.add_argument('--pred-detail', required=True, help='预测细分CSV，需要 样本号/标签')
    ap.add_argument('--pred-summary', required=True, help='预测汇总CSV，需要 样本号/帖子ID（文本）')
    ap.add_argument('--gold-overview', default=DEFAULT_GOLD_OV)
    ap.add_argument('--gold-evidence', default=DEFAULT_GOLD_EV)
    ap.add_argument('--outdir', default='', help='默认预测细分表所在目录')
    ap.add_argument('--tag', default='', help='输出文件名后缀；默认从命中阈值自动识别')
    a = ap.parse_args()
    try:
        result = compare(os.path.abspath(a.pred_detail), os.path.abspath(a.pred_summary),
                         os.path.abspath(a.gold_overview), os.path.abspath(a.gold_evidence),
                         os.path.abspath(a.outdir) if a.outdir else os.path.dirname(os.path.abspath(a.pred_detail)), a.tag)
    except ValueError as e:
        print('!! %s' % e)
        return 2
    print('对比完成：%d 个样本纳入，异常 %d 个' % (result['samples'], result['anomalies']))
    print('TP=%d FP=%d FN=%d  P=%s R=%s F1=%.3f' %
          (result['tp'], result['fp'], result['fn'], pct(result['p']), pct(result['r']), result['f1']))
    print('报告: %s' % result['report'])
    print('明细: %s' % result['detail'])
    print('逐帖汇总: %s' % result['sample'])
    print('异常清单: %s' % result['anomaly'])
    return 0 if result['samples'] else 1


if __name__ == '__main__':
    sys.exit(main())
