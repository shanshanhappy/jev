# -*- coding: utf-8 -*-
r"""refactor_gold.py  金标准模板校验 + 版本快照 + 预测侧空模板落盘（docs\数据处理方案.md §10.1/§10.2/§10.3）

职责（不做任何内容改写，金标准"原模板不动"是方案决策）:
  A. 校验: 金标准 词库原始行号-1 == 词库数据行号 的三元组(类别/细分类别/标签)是否逐行一致,
     以及 (样本号,行号) 重复、行号越界、与汇总表样本号对不齐 —— 输出 out\金标准校验报告.txt
  B. 快照: 复制为带版本号的正式金标准文件(当前=LLM初稿, 人审后由业务重新出 v2)
  C. 模板: 落盘预测侧 17/12 列空表头模板, 让新 schema 在跑 Jev 之前就能被查看/评审

用法:
  python tools\refactor_gold.py
"""
import csv, json, os, sys
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')
csv.field_size_limit(10 * 1024 * 1024)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_SUB = os.path.join(ROOT, '原始资料', '金标准-细分-v1-LLM初稿.csv')
SRC_SUM = os.path.join(ROOT, '原始资料', '金标准-汇总-v1-LLM初稿.csv')
LEXF    = os.path.join(ROOT, 'data', 'label_lexicon.json')
OUTD    = os.path.join(ROOT, 'out')

SUB17 = ['样本号','平台','词库原始行号','标签ID','类别','细分类别','标签','评价/提及对象',
         '表达类型','情绪','证据字段','原文证据','Jev概率','命中阈值','情绪来源','模型版本','API来源']
SUM12 = ['样本号','平台','帖子ID（文本）','CSV记录序号','发布时间','文本字符数','入样原因',
         '部分标签','标签证据数','灰区待复核数','截断标记','原帖链接']

def main():
    lex = json.load(open(LEXF, encoding='utf-8'))
    labels = {L['idx']: L for L in lex['labels']}
    rep = []
    rep.append('金标准校验报告（refactor_gold.py）')
    rep.append('原则: 金标准模板不做任何改写(方案§10.1), 本脚本只校验+快照+出空模板\n')

    # ---------- A. 细分表校验 ----------
    rows = list(csv.DictReader(open(SRC_SUB, encoding='utf-8-sig')))
    sub_cols = list(rows[0].keys())
    bad_rowno = mismatch = 0
    seen = Counter()
    exact = Counter()
    mm_list = []
    empties = []
    samples = set()
    byname = {}
    for L0 in lex['labels']:
        byname.setdefault((L0['cat'], L0['sub'], L0['label']), []).append(L0['idx'])
    for i, r in enumerate(rows, 2):          # 文件行号(含表头)
        s = r['样本号']; samples.add(s)
        seen[(s, r['词库原始行号'])] += 1
        exact[tuple((r[c] or '').strip() for c in ('样本号','标签ID','类别','细分类别','标签','原文证据'))] += 1
        v = (r['词库原始行号'] or '').strip()
        if not v.isdigit():
            bad_rowno += 1; empties.append((i, r)); continue
        idx = int(v) - 1
        L = labels.get(idx)
        if not L:
            bad_rowno += 1; empties.append((i, r)); continue
        if (L['cat'], L['sub'], L['label']) != (r['类别'], r['细分类别'], r['标签']):
            mismatch += 1
            if len(mm_list) < 40:
                mm_list.append('文件第%d行 %s 行号%s: 金标准[%s|%s|%s] != 词库[%s|%s|%s]'
                               % (i, s, r['词库原始行号'], r['类别'], r['细分类别'], r['标签'],
                                  L['cat'], L['sub'], L['label']))
    dup = sum(c - 1 for c in seen.values() if c > 1)
    exact_dup = sum(c - 1 for c in exact.values() if c > 1)
    amb = sum(1 for i, r in empties if len(byname.get((r['类别'], r['细分类别'], r['标签']), [])) > 1)
    dead = sum(1 for i, r in empties if not byname.get((r['类别'], r['细分类别'], r['标签']), []))
    catcnt = Counter(r['类别'] for i, r in empties)
    rep.append('[细分表] 共 %d 行' % len(rows))
    rep.append('  词库行号-1 三元组不一致 : %d   <- 0 = 映射规则成立' % mismatch)
    rep.append('  行号为多行列表/无效    : %d（重名多行集合=%d, 词库查无=%d）' % (bad_rowno, amb, dead))
    rep.append('    缺失行类别分布: %s' % dict(catcnt))
    rep.append('  (样本号,行号) 重复     : %d  <- 同一标签多条证据, 设计如此(细分=证据粒度)' % dup)
    rep.append('  完全同行重复           : %d  <- 应为 0, >0 需人工看' % exact_dup)
    rep += ['  MISMATCH! ' + m for m in mm_list]

    # ---------- A1. 行号缺失行 -> 候选补全表(不改金标准本体) ----------
    fill = os.path.join(OUTD, '金标准行号补全表.csv')
    with open(fill, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['文件行号', '样本号', '类别', '细分类别', '标签', '候选词库数据行号', '候选数', '处理建议'])
        for i, r in empties:
            cand = byname.get((r['类别'], r['细分类别'], r['标签']), [])
            w.writerow([i, r['样本号'], r['类别'], r['细分类别'], r['标签'],
                        '、'.join(str(c + 1) for c in cand), len(cand),
                        '评测按任一命中' if len(cand) > 1 else ('唯一可补' if cand else '词库查无,需业务定')])
    rep.append('  补全表: %s（人审可照此把列表/缺失行号指定到唯一行）' % fill)

    # ---------- A2. 汇总表校验 ----------
    srows = list(csv.DictReader(open(SRC_SUM, encoding='utf-8-sig')))
    scols = list(srows[0].keys())
    sids = {r['样本号'] for r in srows}
    rep.append('\n[汇总表] 共 %d 行' % len(srows))
    rep.append('  细分有而汇总缺的样本: %s' % (sorted(samples - sids) or '无'))
    rep.append('  汇总有而细分缺的样本: %s' % (sorted(sids - samples) or '无'))
    broken_id = sum(1 for r in srows if 'E+' in (r.get('帖子ID（文本）') or '').upper())
    rep.append('  帖子ID 科学计数法损坏: %d 个 (方案§11-G8, 对齐一律用CSV记录序号)' % broken_id)
    human = sum(1 for r in rows if (r.get('人工判定') or '').strip())
    rep.append('  人工判定已填写: %d/%d 行 -> %s' % (human, len(rows),
                 '已有修订' if human else '全部未审, 当前只能当 v1-LLM初稿 用(§11-G7)'))

    # ---------- B. 版本快照(原样复制, 带版本号) ----------
    tag = 'v1-LLM初稿' if human == 0 else 'v?-含人工修订'
    dst_sub = SRC_SUB if ('v1-LLM初稿' in os.path.basename(SRC_SUB)) else os.path.join(OUTD, '金标准-细分-%s.csv' % tag)
    dst_sum = SRC_SUM if ('v1-LLM初稿' in os.path.basename(SRC_SUM)) else os.path.join(OUTD, '金标准-汇总-%s.csv' % tag)
    for src, dst, cols in ((SRC_SUB, dst_sub, sub_cols), (SRC_SUM, dst_sum, scols)):
        if os.path.abspath(src) == os.path.abspath(dst):
            continue                      # 源文件本身已带版本名, 不再复制(防止同文件读写清空)
        with open(src, encoding='utf-8-sig', newline='') as fi, \
             open(dst, 'w', encoding='utf-8-sig', newline='') as fo:
            rd = csv.DictReader(fi); wr = csv.DictWriter(fo, fieldnames=cols)
            wr.writeheader()
            for rr in rd:
                wr.writerow(rr)
    rep.append('\n[快照] %s\n       %s' % (dst_sub, dst_sum))

    # ---------- C. 预测侧空模板 ----------
    t1 = os.path.join(OUTD, '模板-打标结果细分-Jev-17列.csv')
    t2 = os.path.join(OUTD, '模板-打标结果汇总-Jev-12列.csv')
    for cols, dst in ((SUB17, t1), (SUM12, t2)):
        with open(dst, 'w', encoding='utf-8-sig', newline='') as f:
            csv.writer(f).writerow(cols)
    rep.append('[模板] %s\n       %s  (列定义见 方案§10.2/§10.3; 正式文件由 jev_eval.py 产出)' % (t1, t2))

    rpt = os.path.join(OUTD, '金标准校验报告.txt')
    open(rpt, 'w', encoding='utf-8').write('\n'.join(rep) + '\n')
    print('\n'.join(rep))
    print('\n报告已写入:', rpt)


if __name__ == '__main__':
    sys.exit(main() or 0)