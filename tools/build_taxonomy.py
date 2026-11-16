# -*- coding: utf-8 -*-
r"""build_taxonomy.py  生成“Jev整理版标签体系”与“原始行映射”

原则：
- 原始词库 CSV 只读，绝不修改；
- 同(类别,细分类别,TOPIC ANGLE)聚合为一个标签概念；
- TTL/集合汇总节点保留在整理版中，但标记为不参与Jev提问；
- 主文件用于阅读/评审；映射文件保留678条原始行与qid的对应关系，便于回溯。

用法：python tools\build_taxonomy.py
前置：python tools\build_lexicon.py
输出：
  data\标签体系_Jev整理版.csv      645个概念（638个参与提问 + 7个TTL）
  data\标签体系_原始行映射.csv     678条原始行 → 整理版标签/qid
"""
import csv, json, os, sys
sys.stdout.reconfigure(encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'core'))
from jev_rules import merge_label_rows, is_ttl

SRC = os.path.join(ROOT, 'data', 'label_lexicon.json')
OUT_MAIN = os.path.join(ROOT, 'data', '标签体系_Jev整理版.csv')
OUT_MAP = os.path.join(ROOT, 'data', '标签体系_原始行映射.csv')
MAIN_COLS = ['标签问题ID', '类别', '细分类别', 'TOPIC ANGLE', '标签层级', '参与Jev提问',
             '代表词库原始行号', '全部词库原始行号', '重复原始行数',
             '【社媒】命中关键词', '【社媒】同时命中（且）', '【社媒】排除关键词（差）',
             '【社媒】正向同时命中词', '【社媒】负向同时命中词', 'near rule']
MAP_COLS = ['原始数据行号', '词库原始行号', '标签问题ID', '类别', '细分类别', 'TOPIC ANGLE',
            '标签层级', '代表词库原始行号', '重复原始行数',
            '【社媒】命中关键词', '【社媒】同时命中（且）', '【社媒】排除关键词（差）',
            '【社媒】正向同时命中词', '【社媒】负向同时命中词', 'near rule']

def joined(vals):
    return '、'.join(vals)

def main():
    raw = json.load(open(SRC, encoding='utf-8'))['labels']
    leaf = merge_label_rows(raw)                    # 638组，已排除TTL
    groups = leaf + [dict(L, idxs=[L['idx']], members=[L]) for L in raw if is_ttl(L)]
    groups.sort(key=lambda g: g['idx'])
    by_key = {(g['cat'], g['sub'], g['label']): g for g in groups}

    with open(OUT_MAIN, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(MAIN_COLS)
        for g in groups:
            ttl = is_ttl(g)
            w.writerow(['' if ttl else 'q%04d' % g['idx'], g['cat'], g['sub'], g['label'],
                        '汇总节点' if ttl else '叶子标签', '否' if ttl else '是',
                        g['idx'] + 1, joined(str(i + 1) for i in g['idxs']), len(g['idxs']),
                        joined(g['hits']), joined(g['ands']), joined(g['excls']),
                        joined(g['poss']), joined(g['negs']), g['near']])

    with open(OUT_MAP, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(MAP_COLS)
        for L in raw:
            g = by_key[(L['cat'], L['sub'], L['label'])]
            w.writerow([L['idx'], L['idx'] + 1, '' if is_ttl(L) else 'q%04d' % g['idx'],
                        L['cat'], L['sub'], L['label'], '汇总节点' if is_ttl(L) else '叶子标签',
                        g['idx'] + 1, len(g['idxs']), joined(L['hits']), joined(L['ands']),
                        joined(L['excls']), joined(L['poss']), joined(L['negs']), L['near']])

    qids = ['q%04d' % g['idx'] for g in leaf]
    assert len(groups) == 645, len(groups)
    assert len(leaf) == 638, len(leaf)
    assert len(qids) == len(set(qids))
    assert len(raw) == 678
    print('整理版: %s' % OUT_MAIN)
    print('  共645个概念 = 638个Jev问题 + 7个TTL汇总节点')
    print('映射表: %s' % OUT_MAP)
    print('  共678条原始行；同三元组重复8组/41行；原始文件未修改')

    try:
        sys.path.insert(0, os.path.join(ROOT, 'core'))
        import genlog
        genlog.record('tools/build_taxonomy.py', '生成标签体系整理版', OUT_MAIN, 'file',
                      len(groups), note='人读版；改它不影响打标（打标读 label_lexicon.json）',
                      params={'概念数': len(groups), 'Jev问题': len(leaf), '原始行': len(raw)})
        genlog.record('tools/build_taxonomy.py', '生成原始行映射表', OUT_MAP, 'file', len(raw),
                      note='678 原始行 ↔ qid 对照', params={'原始行': len(raw)})
    except Exception as _e:
        print('!! 生成日志写入失败(不影响结果): %s' % _e)

if __name__ == '__main__':
    sys.exit(main() or 0)