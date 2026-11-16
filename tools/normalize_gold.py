# -*- coding: utf-8 -*-
"""normalize_gold.py  金标准细分表归一化 + 去重

解决的问题（2026-11-16 核查 2486 行金标准发现）：
  1) 词库把同一概念拆成多个行号（8 组，如 功效/抗老/改善暗沉 = 130、138），
     merge_label_rows 已合并成 638 个概念，但金标准仍按原始行号引用。
     后果：模型只回答组代表 qid，落在"非代表行号"上的 158 个金标准标签永远命中不了，
     召回率被低估约 3 个百分点。
  2) 同一 (样本, 标签, 对象) 有多行（同标签在不同句子里各标一次），unit 级统计虚高。

处理规则：
  - 每个行号归一为所属概念的组代表行号（idx+1 写回"词库原始行号"列）
  - 原值保留到新列"原始词库行号（未归一）"，可回溯
  - 同一 (样本号, 归一行号, 评价/提及对象) 只保留一行；多条证据用 " | " 合并
  - 不同对象的同标签行（如品牌提及一帖多品牌）全部保留

去重口径（重要）：
  唯一判据是「样本 + 标签 + 对象」。**原文证据不参与重复判定** —— 证据是 LLM 生成的，
  同一标签同一对象在不同句子里各写一段，仍然是重复，只把证据合并到一行。
  反过来，同一标签挂在不同对象上（一帖提到多个产品/多个品牌）是有效记录，必须保留。

用法：
  python tools\\normalize_gold.py                    # 输出 *_归一版.csv，不动原文件
  python tools\\normalize_gold.py --in-place         # 覆盖原文件（先自动备份）
  python tools\\normalize_gold.py --dry-run          # 只打印统计
  python tools\\normalize_gold.py --obj-loose        # 对象名去括注后比对(默认关)
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys

sys.stdout.reconfigure(encoding='utf-8')
csv.field_size_limit(10 * 1024 * 1024)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
from jev_rules import merge_label_rows

LEXF = os.path.join(ROOT, 'data', 'label_lexicon.json')
DEF_SRC = os.path.join(ROOT, '原始资料', '金标准-细分-v1-LLM初稿.csv')
NEW_COL = '原始词库行号（未归一）'
EVID_SEP = ' | '


def build_rep():
    """行号 idx -> 所属概念的组代表 idx"""
    lex = json.load(open(LEXF, encoding='utf-8'))
    rep = {}
    for G in merge_label_rows(lex['labels']):
        for i in G['idxs']:
            rep[i] = G['idx']
    return rep


BRACKET = re.compile(r'[（(].*?[）)]')


def obj_key(s, loose=False):
    """对象名归一。loose=True 时去掉括注（如「精华（图中用户反馈）」→「精华」）"""
    s = (s or '').strip()
    if loose:
        s = BRACKET.sub('', s).strip()
    return s


def normalize(src, dst, in_place=False, dry_run=False, obj_loose=False):
    rep = build_rep()
    rows = list(csv.DictReader(open(src, encoding='utf-8-sig', newline='')))
    if not rows:
        print('!! 空文件: %s' % src)
        return 1
    cols = list(rows[0].keys())
    if NEW_COL not in cols:
        cols_out = cols + [NEW_COL]
    else:
        cols_out = cols

    keep, order = {}, []
    stat = {'rows': len(rows), 'dropped': 0, 'renumbered': 0,
            'multi_line_no': 0, 'evidence_merged': 0}
    for r in rows:
        v = (r.get('词库原始行号') or '').strip()
        nums = [int(x) - 1 for x in re.findall(r'\d+', v)]
        if not nums:
            print('!! 行号为空, 跳过: 样本=%s 标签=%s' % (r.get('样本号'), r.get('标签')))
            continue
        if len(nums) > 1:
            stat['multi_line_no'] += 1
        norm = sorted(set(rep.get(n, n) for n in nums))
        if norm != sorted(nums):
            stat['renumbered'] += 1

        nr = dict(r)
        nr[NEW_COL] = v
        nr['词库原始行号'] = '、'.join(str(n + 1) for n in norm)

        key = (r.get('样本号', ''), tuple(norm), obj_key(r.get('评价/提及对象'), obj_loose))
        if key in keep:
            stat['dropped'] += 1
            old = keep[key]
            ev_new = (r.get('原文证据') or '').strip()
            if ev_new and ev_new not in old['原文证据']:
                old['原文证据'] = (old['原文证据'] + EVID_SEP + ev_new).strip(EVID_SEP + ' ')
                stat['evidence_merged'] += 1
        else:
            keep[key] = nr
            order.append(key)

    out = [keep[k] for k in order]
    pairs = len({(r['样本号'], i) for r in out
                 for i in (int(x) - 1 for x in re.findall(r'\d+', r['词库原始行号']))})
    print('原文件 %s' % src)
    print('  行数 %d -> %d  （删除重复 %d 行）' % (stat['rows'], len(out), stat['dropped']))
    print('  行号被归一的行 %d 条；含多个行号的行 %d 条' % (stat['renumbered'], stat['multi_line_no']))
    print('  合并证据 %d 处（用 " | " 连接，无信息丢失）' % stat['evidence_merged'])
    print('  唯一 (样本, 标签) 对 %d' % pairs)
    if not obj_loose:
        loose_n = len({(r.get('样本号', ''),
                        tuple(sorted(set(int(x) - 1 for x in re.findall(r'\d+', r['词库原始行号'])))),
                        obj_key(r.get('评价/提及对象'), True)) for r in out})
        if loose_n < len(out):
            print('  提示: 对象名去掉括注后可再合并 %d 行（如「X（图中用户反馈）」≈「X」），'
                  '确需合并请加 --obj-loose' % (len(out) - loose_n))

    if dry_run:
        print('  [dry-run] 未写文件')
        return 0

    if in_place:
        bak = src.replace('.csv', '.bak-%s.csv' % __import__('time').strftime('%Y%m%d-%H%M%S'))
        if not os.path.exists(bak):
            shutil.copy2(src, bak)
            print('  已备份: %s' % os.path.basename(bak))
        target = src
    else:
        target = dst

    tmp = target + '.tmp'
    with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols_out, extrasaction='ignore')
        w.writeheader()
        w.writerows(out)
    os.replace(tmp, target)
    print('  已写入: %s' % target)
    try:
        import genlog
        genlog.record('tools/normalize_gold.py', '归一化金标准', target, 'file', len(out),
                      note='删 %d 行重复（同标签同对象才删，证据不参与判断）' % stat['dropped'],
                      params={'原行数': stat['rows'], '现行数': len(out),
                              '唯一标签对': pairs, '行号被归一': stat['renumbered'],
                              'obj_loose': obj_loose})
    except Exception as _e:
        print('!! 生成日志写入失败(不影响结果): %s' % _e)
    return 0


def main():
    ap = argparse.ArgumentParser('金标准归一化 + 去重')
    ap.add_argument('--src', default=DEF_SRC)
    ap.add_argument('--dst', default='', help='输出路径; 默认与源同目录的 *_归一版.csv')
    ap.add_argument('--in-place', action='store_true', help='覆盖原文件(自动备份)')
    ap.add_argument('--obj-loose', action='store_true',
                    help='对象名去括注后比对，如「X（图中用户反馈）」与「X」视为同一对象（默认关）')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    dst = a.dst or a.src.replace('.csv', '_归一版.csv')
    return normalize(a.src, dst, a.in_place, a.dry_run, a.obj_loose)


if __name__ == '__main__':
    sys.exit(main() or 0)
