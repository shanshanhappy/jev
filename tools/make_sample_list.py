# -*- coding: utf-8 -*-
r"""make_sample_list.py  生成"抽样清单"(打标管道的正式输入之一)
职责边界:
  - 抽样清单 = 谁入选、为什么入选、第几行数据 —— 属于【抽样阶段】的账本;
  - 金标准(答卷) = 标签结果 —— 属于【标注阶段】, 只允许 jev_eval 在评测时读。
冒烟期清单可从金标准汇总导出一次(本脚本默认动作); 生产批量时由抽样脚本直接产出同格式文件,
不再回读金标准。

用法:
  python tools\make_sample_list.py                                   # 从金标准汇总导出
  python tools\make_sample_list.py --src 某批次抽样记录.csv --out data\抽样清单_批次2.csv
输出列: 样本号, 平台, CSV记录序号, 入样原因
"""
import argparse, csv, os, sys
sys.stdout.reconfigure(encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLS = ['样本号', '平台', 'CSV记录序号', '入样原因']

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=os.path.join(ROOT, '原始资料', '金标准-汇总-v1-LLM初稿.csv'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', '抽样清单.csv'))
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.src, encoding='utf-8-sig')))
    with open(a.out, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow({c: (r.get(c) or '').strip() for c in COLS})
    n_no_reason = sum(1 for r in rows if not (r.get('入样原因') or '').strip())
    print('清单已生成: %s  (%d 条, 缺入样原因 %d 条)' % (a.out, len(rows), n_no_reason))
    print('注: 金标准今后只被 jev_eval 当"答卷"读取, 不再作为管道输入。')

if __name__ == '__main__':
    sys.exit(main())