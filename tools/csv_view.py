# -*- coding: utf-8 -*-
r"""csv_view.py  CSV 单行化预览器(不改动原文件)
CSV 字段里含换行是合法的(引号包裹), 但编辑器会按物理换行显示, 看起来"一行数据占好多行"。
本工具把内部换行替换成 ⏎、按列截断, 输出到终端或平铺副本 CSV。

用法:
  python tools\csv_view.py data\data_export_clean_v3.csv                  # 终端看前5行(截断显示)
  python tools\csv_view.py data\data_export_clean_v3.csv -n 20 --cols id,site_name,csv_row_no,char_count_raw
  python tools\csv_view.py data\data_export_clean_v3.csv --flat out\data_export_clean_v3_flat.csv   # 全量平铺副本
"""
import argparse, csv, sys
sys.stdout.reconfigure(encoding='utf-8')
csv.field_size_limit(50 * 1024 * 1024)

def flat(v, w=None):
    v = (v or '').replace('\r\n', '⏎').replace('\n', '⏎').replace('\r', '⏎').replace('\t', ' ')
    if w and len(v) > w:
        v = v[:w] + '…'
    return v

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file')
    ap.add_argument('-n', type=int, default=5, help='终端预览条数')
    ap.add_argument('--cols', default='', help='只看这些列, 逗号分隔; 默认全部')
    ap.add_argument('--width', type=int, default=60, help='终端显示时字段截断宽度')
    ap.add_argument('--flat', default='', help='写出全量平铺副本的 CSV 路径(字段内换行->⏎)')
    a = ap.parse_args()

    rd = csv.DictReader(open(a.file, encoding='utf-8-sig', newline=''))
    cols = [c.strip() for c in a.cols.split(',') if c.strip()] or rd.fieldnames

    if a.flat:
        n = 0
        with open(a.flat, 'w', encoding='utf-8-sig', newline='') as fo:
            wr = csv.writer(fo, lineterminator='\n')
            wr.writerow(rd.fieldnames)
            for row in rd:
                wr.writerow([flat(row.get(c, '')) for c in rd.fieldnames])
                n += 1
        print('已写平铺副本: %s (%d 条, 字段内换行已替换为⏎, 仅供阅读, 勿回灌管道)' % (a.flat, n))
        return 0

    head = [flat(c) for c in cols]
    print(' | '.join(head))
    print('-' * 100)
    for i, row in enumerate(rd):
        if i >= a.n:
            break
        print(' | '.join(flat(row.get(c, ''), a.width) for c in cols))
    print('...')
    total = i + 1 + sum(1 for _ in rd)
    print('逻辑记录总数: %d  (终端每行=1条记录)' % total)
    return 0

if __name__ == '__main__':
    sys.exit(main())