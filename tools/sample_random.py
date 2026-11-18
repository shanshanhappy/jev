# -*- coding: utf-8 -*-
r"""sample_random.py  从 data_export.csv 随机抽 N 条, 生成"抽样清单"

为什么要绕一层 id 映射:
  抽样清单的「CSV记录序号」必须是 v3 的 csv_row_no —— 因为答案表的
  P 号严格等于 csv_row_no(P005913 <=> 第 5913 行)。而 data_export.csv 里
  没有这一列(它是 47 列原始导出, v3 才是清洗+编号后的产物), 所以只能拿
  id 当锚点: data_export.id -> v3.csv_row_no。
  两个文件的 id 集合已验证完全一致(10486 == 10486, 互不缺失), 映射无损。

用法:
  python tools\sample_random.py                                  # 抽 1000 条, 种子 42
  python tools\sample_random.py --n 300 --seed 7 --out data\抽样清单_300.csv
  python tools\sample_random.py --stratify                       # 按平台配额分层(默认纯随机)

输出列与 data\抽样清单.csv 完全一致, 下游 schemes.py / jev_eval.py 无需改动。
"""
import argparse
import csv
import os
import random
import sys
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')
csv.field_size_limit(10 ** 9)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLS = ['样本号', '平台', 'CSV记录序号', '入样原因']


def read_ids(path):
    """data_export.csv -> [(id, site_name)], 保持文件顺序"""
    out = []
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            i = (r.get('id') or '').strip()
            if i:
                out.append((i, (r.get('site_name') or '').strip()))
    return out


def read_rowno(path):
    """v3 -> {id: csv_row_no}"""
    m = {}
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            i = (r.get('id') or '').strip()
            n = (r.get('csv_row_no') or '').strip()
            if i and n.isdigit():
                m[i] = int(n)
    return m


def main():
    ap = argparse.ArgumentParser('从 data_export.csv 随机抽 N 条生成抽样清单')
    ap.add_argument('--src', default=os.path.join(ROOT, 'data', 'data_export.csv'))
    ap.add_argument('--v3', default=os.path.join(ROOT, 'data', 'data_export_clean_v3.csv'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', '抽样清单_1000.csv'))
    ap.add_argument('--n', type=int, default=1000, help='抽多少条')
    ap.add_argument('--seed', type=int, default=42, help='随机种子; 同种子同结果, 便于复现')
    ap.add_argument('--stratify', action='store_true',
                    help='按平台配额分层(配额=全量占比); 默认纯随机, 大样本下平台分布自动贴合全量')
    a = ap.parse_args()

    rows = read_ids(a.src)
    id2no = read_rowno(a.v3)
    print('源文件 %s: %d 行, 唯一 id %d' % (
        os.path.basename(a.src), len(rows), len({x[0] for x in rows})))

    # 只抽能在 v3 里找到行号的 id, 否则生成的清单下游直接报"无匹配样本"
    pool = [(i, p) for i, p in rows if i in id2no]
    dropped = len(rows) - len(pool)
    if dropped:
        print('!! 有 %d 条在 v3 里找不到 csv_row_no, 已排除' % dropped)
    if a.n > len(pool):
        print('!! 要抽 %d 条, 但池子只有 %d 条' % (a.n, len(pool)))
        return 1

    random.seed(a.seed)

    if a.stratify:
        # 平台配额按全量占比分配, 最大余数法补齐, 保证合计正好等于 n
        cnt = Counter(p for _, p in pool)
        total = len(pool)
        quota = {p: a.n * c / total for p, c in cnt.items()}
        take = {p: int(v) for p, v in quota.items()}
        left = a.n - sum(take.values())
        for p, _ in sorted(quota.items(), key=lambda kv: -(kv[1] - int(kv[1])))[:left]:
            take[p] += 1
        picked = []
        for p in cnt:
            cand = [x for x in pool if x[1] == p]
            picked.extend(random.sample(cand, take[p]))
        print('分层抽样: %s' % ', '.join('%s=%d' % (p, take[p]) for p in cnt))
    else:
        picked = random.sample(pool, a.n)
        print('纯随机抽样: %d 条 (种子 %d)' % (a.n, a.seed))

    # 按 v3 行号升序编号 S001..Sn —— 清单有序, 跑批时进度也好看
    picked.sort(key=lambda x: id2no[x[0]])

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for k, (i, p) in enumerate(picked, 1):
            w.writerow({
                '样本号': 'S%03d' % k if k < 1000 else 'S%d' % k,
                '平台': p,
                'CSV记录序号': id2no[i],
                '入样原因': '从 data_export.csv 全量随机抽取(种子%d)' % a.seed,
            })

    print('\n清单已生成: %s  (%d 条)' % (a.out, len(picked)))
    pc = Counter(p for _, p in picked)
    for p, c in pc.most_common():
        print('  %-10s %4d (%.1f%%)' % (p, c, 100 * c / len(picked)))
    print('记录序号范围: %d ~ %d' % (min(id2no[i] for i, _ in picked),
                                    max(id2no[i] for i, _ in picked)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
