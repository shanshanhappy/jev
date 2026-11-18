# -*- coding: utf-8 -*-
r"""
check_align.py -- 样本对齐闸门（只读，不改任何数据）

回答一个问题: 我跑的这 100 条预测，和「验证正确数据_v0.2.1」这份答案，
              是不是**同一个帖子**在比？错一条都不行。

三套编号必须串成一条链，缺任何一环就会"静默算错"（不报错、指标全是垃圾）:

    抽样清单.样本号  S001..S100        <- 流水线内部编号，只在本批有意义
        └─ 抽样清单.CSV记录序号  n
             ├─ data_export_clean_v3.csv 的 csv_row_no == n  ->  取 id
             └─ 验证正确数据_全量打标总览.csv 的 CSV记录序号 == n
                  ├─ 帖子ID（文本）  必须 == v3 的 id      <- 最硬的锚点（10,486 个唯一值）
                  └─ 样本号  P000001..P010486             <- 答案表的编号
                       └─ 验证正确数据_标签与证据.csv 按此样本号取 (类别,细分类别,标签,词库原始行号)

四道检查，前两道防"接错表"，后两道是"内容级铁证"——不靠编号，直接比原文:

  ① 行号连续 / id 唯一 / 样本号唯一
  ② 同一 CSV记录序号下，v3.id == 答案帖子ID
  ③ 抽样清单每一条都能落到 v3 和答案上，S 号 ↔ P 号 无折叠
  ④ 内容级: 答案表的「原文证据」必须能在对应的 v3 正文里逐字搜到
     （做完 ①②③ 是"编号对得上"，做完 ④ 才是"确实是同一篇文章"）

用法:
  python tools\check_align.py                       # 校验默认的 data\抽样清单.csv
  python tools\check_align.py --list data\抽样清单.csv
  python tools\check_align.py --out out\样本对齐表.csv
  python tools\check_align.py --strict-list          # 抽样清单样本在答案里没标签也判失败
  python tools\check_align.py --min-ev 0.95          # 内容级命中率低于 95% 判失败

退出码: 0=全部通过  1=有致命不一致  2=文件缺失
"""
import argparse
import csv
import io
import os
import re
import sys
import unicodedata

sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
csv.field_size_limit(10 * 1024 * 1024)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V3F = os.path.join(ROOT, 'data', 'data_export_clean_v3.csv')
OVF = os.path.join(ROOT, 'data', '验证正确数据_v0.2.1_全量打标总览.csv')
EVF = os.path.join(ROOT, 'data', '验证正确数据_v0.2.1_标签与证据.csv')
SLF = os.path.join(ROOT, 'data', '抽样清单.csv')


def norm_text(s):
    """归一化：全角转半角、去所有空白、去标点、小写。
    答案里的证据常写成「珀莱雅（PROYA）」，正文是「珀莱雅(PROYA)」，
    不归一化会误判成"对不上"。"""
    s = unicodedata.normalize('NFKC', s or '')
    s = re.sub(r'[\s\u3000]+', '', s)
    s = re.sub(r'[^\w\u4e00-\u9fff]', '', s)
    return s.lower()


def read_csv_after_title(path, title_rows):
    """跳过表头前的 title_rows 行大标题/说明，返回 (header, rows)"""
    with io.open(path, encoding='utf-8-sig', newline='') as f:
        rd = csv.reader(f)
        for _ in range(title_rows):
            next(rd, None)
        header = next(rd)
        return header, [r for r in rd if r and any(x.strip() for x in r)]


class Report(object):
    def __init__(self):
        self.fatal = []
        self.warn = []
        self.info = []

    def bad(self, msg):
        self.fatal.append(msg)

    def warn_(self, msg):
        self.warn.append(msg)

    def ok(self, msg):
        self.info.append('[OK] ' + msg)


def load_v3(rep):
    """返回 (id_map, text_map)，键都是 csv_row_no"""
    if not os.path.exists(V3F):
        print('!! 缺少 %s\n   先跑: python tools\\build_clean.py' % V3F)
        sys.exit(2)
    m = {}
    txt = {}
    dup = 0
    with io.open(V3F, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            try:
                n = int(r['csv_row_no'])
            except (KeyError, ValueError):
                continue
            if n in m:
                dup += 1
            m[n] = r['id']
            txt[n] = r.get('full_text') or ''
    nums = sorted(m)
    rep.ok('v3: %d 行，csv_row_no 范围 %d..%d' % (len(m), nums[0], nums[-1]))
    if nums != list(range(1, len(nums) + 1)):
        rep.bad('v3 的 csv_row_no 不是 1..N 连续，抽样清单的行号可能已失效')
    if dup:
        rep.bad('v3 有 %d 个重复的 csv_row_no' % dup)
    if len(set(m.values())) != len(m):
        rep.bad('v3 的 id 不唯一（%d 个唯一 / %d 行）' % (len(set(m.values())), len(m)))
    else:
        rep.ok('v3: id 唯一 %d 个' % len(m))
    return m, txt


def load_overview(rep):
    if not os.path.exists(OVF):
        print('!! 缺少 %s' % OVF)
        sys.exit(2)
    header, rows = read_csv_after_title(OVF, 0)
    ix = {c: i for i, c in enumerate(header)}
    for need in ('样本号', '帖子ID（文本）', 'CSV记录序号'):
        if need not in ix:
            print('!! 总览表缺列: %s（实际列: %s）' % (need, header))
            sys.exit(2)
    m = {}
    for r in rows:
        try:
            n = int(r[ix['CSV记录序号']])
        except (ValueError, IndexError):
            continue
        m[n] = (r[ix['样本号']].strip(), r[ix['帖子ID（文本）']].strip())
    rep.ok('答案总览: %d 行，CSV记录序号范围 %d..%d'
           % (len(m), min(m), max(m)))
    pnos = [v[0] for v in m.values()]
    if len(set(pnos)) != len(pnos):
        rep.bad('答案总览的样本号不唯一')
    else:
        rep.ok('答案总览: 样本号唯一 %d 个（例 %s）' % (len(pnos), min(pnos)))
    ids = [v[1] for v in m.values()]
    if len(set(ids)) != len(ids):
        rep.bad('答案总览的帖子ID 不唯一（%d / %d）' % (len(set(ids)), len(ids)))
    else:
        rep.ok('答案总览: 帖子ID 唯一 %d 个' % len(ids))
    return m


def load_evidence(rep):
    """返回 (每帖标签数, {样本号: [(标签, 证据字段, 原文证据), ...]})"""
    if not os.path.exists(EVF):
        print('!! 缺少 %s' % EVF)
        sys.exit(2)
    header, rows = read_csv_after_title(EVF, 2)   # 本文件表头前有 2 行标题/说明
    ix = {c: i for i, c in enumerate(header)}
    for need in ('样本号', '类别', '细分类别', '标签', '词库原始行号'):
        if need not in ix:
            print('!! 标签与证据缺列: %s（实际列: %s）' % (need, header))
            sys.exit(2)
    cnt = {}
    detail = {}
    no_rowno = 0
    has_ev = '原文证据' in ix
    i_ev, i_fld = ix.get('原文证据'), ix.get('证据字段')
    for r in rows:
        s = r[ix['样本号']].strip()
        if not s:
            continue
        cnt[s] = cnt.get(s, 0) + 1
        if not r[ix['词库原始行号']].strip():
            no_rowno += 1
        if has_ev:
            e = r[i_ev].strip()
            if e:
                fld = r[i_fld].strip() if i_fld is not None else ''
                detail.setdefault(s, []).append((r[ix['标签']].strip(), fld, e))
    total = sum(cnt.values())
    rep.ok('答案明细: %d 行 / %d 个帖子，平均 %.2f 标签/帖'
           % (total, len(cnt), total / len(cnt) if cnt else 0))
    if no_rowno:
        rep.warn_('答案明细有 %d 行缺「词库原始行号」，将回退到按三元组匹配' % no_rowno)
    else:
        rep.ok('答案明细: 词库原始行号 100% 填充，可精确对齐到标签 idx')
    if not has_ev:
        rep.warn_('答案明细没有「原文证据」列，跳过内容级验证')
    return cnt, detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', dest='lst', default=SLF, help='抽样清单CSV')
    ap.add_argument('--out', dest='out', default=os.path.join(ROOT, 'out', '样本对齐表.csv'))
    ap.add_argument('--strict-list', action='store_true',
                    help='抽样清单里的样本在答案中无标签时也判失败')
    ap.add_argument('--min-ev', dest='min_ev', type=float, default=0.90,
                    help='内容级验证的最低命中率（默认 0.90），低于此值判失败')
    a = ap.parse_args()

    rep = Report()
    print('=' * 78)
    print('样本对齐闸门  check_align.py   清单=%s' % os.path.basename(a.lst))
    print('=' * 78)

    v3, v3txt = load_v3(rep)
    ov = load_overview(rep)
    evcnt, evdetail = load_evidence(rep)

    # ---- 核心 1: v3.id 与答案总览的帖子ID，按同一个 csv_row_no 必须完全相等 ----
    both = sorted(set(v3) & set(ov))
    mism = [(n, v3[n], ov[n][1]) for n in both if v3[n] != ov[n][1]]
    only_v3 = sorted(set(v3) - set(ov))
    only_ov = sorted(set(ov) - set(v3))
    if mism:
        rep.bad('v3.id 与答案帖子ID 有 %d 条不一致（同一 CSV记录序号）：' % len(mism))
        for n, x, y in mism[:5]:
            rep.bad('    第 %d 行  v3=%s  答案=%s' % (n, x, y))
    else:
        rep.ok('核心锚点: 同一 CSV记录序号下，v3.id == 答案帖子ID，%d 条全部一致' % len(both))
    if only_v3:
        rep.bad('有 %d 行只在 v3 里、答案里没有（如 %s）' % (len(only_v3), only_v3[:5]))
    if only_ov:
        rep.bad('有 %d 行只在答案里、v3 里没有（如 %s）' % (len(only_ov), only_ov[:5]))
    if set(v3) == set(ov) and not mism:
        rep.ok('两张表行集合完全相同（%d 行），可安全按 CSV记录序号互查' % len(v3))

    # ---- 核心 2: 抽样清单能否全部落到 v3 与答案上 ----
    if not os.path.exists(a.lst):
        print('!! 缺少抽样清单: %s' % a.lst)
        sys.exit(2)
    sl = list(csv.DictReader(io.open(a.lst, encoding='utf-8-sig')))
    rep.ok('抽样清单: %d 条' % len(sl))

    names = [r.get('样本号', '').strip() for r in sl]
    if len(set(names)) != len(names):
        rep.bad('抽样清单的样本号有重复')
    seqs = []
    for r in sl:
        raw = (r.get('CSV记录序号') or '').strip()
        if not raw.isdigit():
            rep.bad('抽样清单有非数字的 CSV记录序号: %s' % (r.get('样本号'),))
            continue
        seqs.append(int(raw))
    if len(set(seqs)) != len(seqs):
        dupn = sorted(n for n in set(seqs) if seqs.count(n) > 1)
        rep.bad('抽样清单的 CSV记录序号有重复: %s（两条清单指向同一帖=重复计分）' % dupn[:10])

    miss_v3 = [n for n in seqs if n not in v3]
    miss_ov = [n for n in seqs if n not in ov]
    if miss_v3:
        rep.bad('抽样清单有 %d 个 CSV记录序号 在 v3 里找不到: %s' % (len(miss_v3), miss_v3[:10]))
    if miss_ov:
        rep.bad('抽样清单有 %d 个 CSV记录序号 在答案里找不到: %s' % (len(miss_ov), miss_ov[:10]))
    if not miss_v3 and not miss_ov:
        rep.ok('抽样清单 %d 条全部能在 v3 与答案中对上' % len(seqs))

    # ---- 核心 3: 逐条列出映射，并做内容级验证 ----
    table = []
    pnos = []
    no_ev = []
    ev_tot = ev_hit = 0
    ev_bad_post = []          # 证据命中率不过半的帖子
    ev_miss_fld = {}          # 未命中证据按「证据字段」归类，便于解释
    for r in sl:
        s = (r.get('样本号') or '').strip()
        raw = (r.get('CSV记录序号') or '').strip()
        if not raw.isdigit():
            continue
        n = int(raw)
        p, pid = ov.get(n, ('', ''))
        if p:
            pnos.append(p)
        k = evcnt.get(p, 0) if p else 0
        if p and k == 0:
            no_ev.append(s)

        # 内容级: 答案的「原文证据」能否在对应帖子的正文里逐字搜到
        body = norm_text(v3txt.get(n, ''))
        items = evdetail.get(p, [])
        hit = 0
        for lab, fld, e in items:
            if norm_text(e) in body:
                hit += 1
            else:
                ev_miss_fld[fld or '(空)'] = ev_miss_fld.get(fld or '(空)', 0) + 1
        ev_tot += len(items)
        ev_hit += hit
        rate = (hit / len(items)) if items else None
        if items and rate is not None and rate < 0.5:
            ev_bad_post.append((s, p, hit, len(items)))
        table.append([s, n, v3.get(n, ''), pid, p, k,
                      '' if rate is None else '%.0f%%' % (100 * rate)])

    if ev_tot:
        overall = ev_hit / ev_tot
        rep.ok('内容级验证: 答案的 %d 条「原文证据」中 %d 条在对应帖子正文里搜到（%.1f%%）'
               % (ev_tot, ev_hit, 100 * overall))
        if ev_bad_post:
            rep.bad('内容级验证: %d 个帖子的证据命中率不足一半，可能不是同一篇：%s'
                    % (len(ev_bad_post), '; '.join('%s->%s %d/%d' % x for x in ev_bad_post[:5])))
        else:
            rep.ok('内容级验证: %d 个帖子的证据命中率均过半，确认是同一篇文章' % len(sl))
        if overall < a.min_ev:
            rep.bad('内容级验证总命中率 %.1f%% 低于阈值 %.0f%%，'
                    '请确认答案表与 v3 是否同一批数据' % (100 * overall, 100 * a.min_ev))
        if ev_miss_fld:
            top = sorted(ev_miss_fld.items(), key=lambda x: -x[1])[:5]
            rep.warn_('未命中的证据按「证据字段」分布: %s'
                      % ', '.join('%s=%d' % x for x in top)
                      + ('（cover_ocr_content=封面图OCR文字，v3 里没有这一列；'
                         'content 未命中多为 URL 已被清洗或序号写法不同）' if ev_miss_fld else ''))

    if len(set(pnos)) != len(pnos):
        rep.bad('本批多个样本指向了同一个答案样本号（映射有折叠）')
    else:
        rep.ok('本批 %d 个样本与答案样本号一一对应，无折叠' % len(pnos))

    if no_ev:
        tag = rep.bad if a.strict_list else rep.warn_
        tag('本批有 %d 个样本在答案里没有标签（%s）：这些帖的"正确答案=空集"，'
            '模型只要输出任何标签就是 FP' % (len(no_ev), ','.join(no_ev[:8])))

    # ---- 输出对照表 ----
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    with io.open(a.out, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['样本号(流水线)', 'CSV记录序号', '帖子ID', '答案帖子ID',
                    '答案样本号', '答案标签数', '证据命中率'])
        w.writerows(table)
    rep.ok('对照表已写入 %s（%d 行）' % (a.out, len(table)))

    # ---- 结论 ----
    print()
    for m in rep.info:
        print('  ' + m)
    print()
    if rep.warn:
        print('  提示:')
        for m in rep.warn:
            print('    [WARN] ' + m)
        print()
    if rep.fatal:
        print('  !! 致命问题 %d 项:' % len(rep.fatal))
        for m in rep.fatal:
            print('    ' + m)
        print()
        print('  结论: 对齐链断了 —— 此时算出的 P/R/F1 不可信，先修映射再跑评测。')
        return 1
    print('  结论: 对齐链完整，可以用这份答案算指标。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
