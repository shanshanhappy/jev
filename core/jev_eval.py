# -*- coding: utf-8 -*-
r"""jev_eval.py  第5步: 原始响应 + 词库 + 金标准 -> 预测两表 + 评测报告
职责(见 docs\数据处理方案.md §10.2~10.5):
  1) 解析 out\raw\{sample}__{scheme}__{mode}.jsonl 的 Noul 概率 -> 按阈值判命中
  2) 词库反查标签四件套; 证据回搜(命中关键词->所在【来源段】与原文窗口); 情绪 near 窗口规则
  3) 输出预测 细分17列 / 汇总12列, 并与金标准按 (样本号, 标签idx) 双键对齐

用法:
  python core\jev_eval.py --scheme B --mode A2 --tau 0.6
  python core\jev_eval.py --scheme B --mode A2            # tau 取 raw_to_csv.DEFAULT_THRESHOLD
  python core\jev_eval.py --scheme A2 --mode A2 --gold new      # 对齐 v0.2.1 人工答案
  python core\jev_eval.py --scheme A2 --mode A2 --gold new --alias   # 再做同名标签归并
输出:
  {outdir}\打标结果细分-Jev_{scheme}_{mode}[_{gold}].csv
  {outdir}\打标结果汇总-Jev_{scheme}_{mode}[_{gold}].csv
  {outdir}\评测报告_{scheme}_{mode}[_{gold}].md
  outdir 默认跟随 --raw-dir；未传 --raw-dir 时仍为 out\
  （同名文件已存在时自动加 _rHHMMSS 后缀, 不会静默覆盖上一批结果）

金标准两种口径 --gold:
  old (默认) 原始资料\金标准-细分-v1-LLM初稿.csv, 样本号 S001..S100, 与本批样本号天然同名
  new        data\验证正确数据_v0.2.1_*.csv, 样本号 P000001.., 需经
             抽样清单.S号 → CSV记录序号 → 总览.P号 → 明细.词库原始行号 → 标签idx 三级映射
             映射链已被 tools\check_align.py 验证(10,486 条帖子ID 全等 / 1,441 条原文证据 98.6% 命中)

2026-11-16 修订:
  1) --scheme 与 --mode 解耦。旧版只认 --mode, 而文件名是 {sample}__{scheme}__{mode}.jsonl,
     传 --mode B 永远匹配不到(0 条), 传 --mode A2 又会把 B 方案的数据读进来。
  2) P/R/F1 统一到 (样本号, 标签idx) 唯一对口径。旧版 TP 按"金标准行"计、FP 按"预测标签"计,
     量纲不同: 金标准同一标签被拆成多行时, 一个预测能把多个 unit 同时判对,
     TP 从 677 虚增到 1189, 准确率 49.5% 被报成 63.2%。
  3) --tau 默认改为读 tools\raw_to_csv.DEFAULT_THRESHOLD(0.6), 与打标明细表同口径。
  4) 修复 markdown 表格分隔符: FN/FP 列表原来用 "|" 连接维度与标签, 会把表格撑成 4 列。
"""
import argparse, csv, json, os, re, sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')
csv.field_size_limit(10 * 1024 * 1024)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D_P, D_R, D_O = (os.path.join(ROOT, x) for x in ('out/payloads', 'out/raw', 'out'))
LEXF = os.path.join(ROOT, 'data', 'label_lexicon.json')
GOLD_SUB = os.path.join(ROOT, '原始资料', '金标准-细分-v1-LLM初稿.csv')
from jev_rules import split_segments, find_evidence, sentiment, FIELD_MAP, merge_label_rows

# 注意: 必须 append 不能 insert(0)。core\ 是脚本目录(已在 sys.path[0])，
# 把 tools\ 插到最前会让 tools\ 下的同名模块盖掉 core\ 的（曾导致 import genlog
# 命中 tools/genlog.py 而报 "no attribute 'record'"）。放到末尾就能找到 raw_to_csv 又不抢优先。
sys.path.append(os.path.join(ROOT, 'tools'))
try:
    from raw_to_csv import DEFAULT_THRESHOLD as _TAU_DEFAULT
except Exception:
    _TAU_DEFAULT = 0.6


def parse_name(fn):
    """S001__B__A2.jsonl -> ('S001','B','A2'); 旧命名 S001.jsonl -> ('S001','','')"""
    if not fn.endswith('.jsonl'):
        return None
    parts = fn[:-6].split('__')
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 1:
        return parts[0], '', ''
    return None


def load_gold(lex, path=None):
    """金标准 -> {sample: [可接受idx集合,...]}; 一个金标准行 = 一个 unit"""
    byname = defaultdict(list)
    for L in lex['labels']:
        byname[(L['cat'], L['sub'], L['label'])].append(L['idx'])
    gold_units = defaultdict(list)
    orphan = amb = 0
    for r in csv.DictReader(open(path or GOLD_SUB, encoding='utf-8-sig')):
        v = (r['词库原始行号'] or '').strip()
        s = (r['样本号'] or '').strip()
        nums = [int(n) - 1 for n in re.findall(r'\d+', v)]
        if nums:
            u = set(nums)
        else:
            cand = byname.get((r['类别'], r['细分类别'], r['标签']), [])
            if not cand:
                orphan += 1
                continue
            u = set(cand)
            amb += 1
        gold_units[s].append(u)
    return gold_units, orphan, amb


# ============================================================
# 新答案 v0.2.1 的加载: 三级映射 S号 → CSV记录序号 → P号 → 词库行号 → idx
# ============================================================
NEW_OV = os.path.join(ROOT, 'data', '验证正确数据_v0.2.1_全量打标总览.csv')
NEW_EV = os.path.join(ROOT, 'data', '验证正确数据_v0.2.1_标签与证据.csv')
SAMPLE_LIST = os.path.join(ROOT, 'data', '抽样清单.csv')


def _csv_after_header(path, need):
    """读 CSV 并自动定位表头行(第一行同时含 need 全部列名者)。

    为什么不写死 skiprows: 新答案里「标签与证据」前面有 2 行标题/说明, 表头在第 3 行,
    而「全量打标总览」表头就在第 1 行。写死行号以后 xlsx 重导一次就错位, 且不报错。
    """
    if not os.path.exists(path):
        return None, []
    with open(path, encoding='utf-8-sig', newline='') as f:
        rd = csv.reader(f)
        header = None
        for row in rd:
            cells = [c.strip() for c in row]
            if need <= set(cells):
                header = cells
                break
        if header is None:
            return None, []
        rows = [r for r in rd if any((c or '').strip() for c in r)]
    return header, rows


def load_gold_new(lex):
    """新答案 -> ({sample: [可接受idx集合]}, orphan, amb, info)

    info 里带对齐体检数字, 由 main 打印, 让「映射到几个样本、有没有撞车」一眼可见。
    """
    byname = defaultdict(list)
    for L in lex['labels']:
        byname[(L['cat'], L['sub'], L['label'])].append(L['idx'])

    info = {'mapped': 0, 'clash': [], 'dup_s': 0, 'no_row': 0, 'samples': 0, 'rows': 0}

    # ① 抽样清单: S号 -> CSV记录序号 (整批 100 条的清单, 也只有它知道 S 号)
    s2row = {}
    if not os.path.exists(SAMPLE_LIST):
        info['err'] = '找不到抽样清单: %s' % SAMPLE_LIST
        return {}, 0, 0, info
    for r in csv.DictReader(open(SAMPLE_LIST, encoding='utf-8-sig')):
        s = (r.get('样本号') or '').strip()
        n = (r.get('CSV记录序号') or '').strip()
        if not s or not n.isdigit():
            continue
        if s in s2row:
            info['dup_s'] += 1
        s2row[s] = int(n)

    # ② 总览: CSV记录序号 -> P号
    h, rows = _csv_after_header(NEW_OV, {'CSV记录序号', '样本号'})
    if h is None:
        info['err'] = '读不到总览表(缺文件或表头不含 CSV记录序号/样本号): %s' % NEW_OV
        return {}, 0, 0, info
    i_n, i_p = h.index('CSV记录序号'), h.index('样本号')
    row2p = {}
    for r in rows:
        n = r[i_n].strip()
        if n.isdigit():
            row2p[int(n)] = r[i_p].strip()

    # ③ P号 -> S号; 两个样本抢同一个 P 号说明行号有重复, 必须报出来
    p2s = {}
    for s, n in s2row.items():
        p = row2p.get(n)
        if not p:
            continue
        if p in p2s and p2s[p] != s:
            info['clash'].append((p, p2s[p], s))
        p2s[p] = s
    info['mapped'] = len(p2s)

    # ④ 明细: P号 -> 词库原始行号 -> idx
    h, rows = _csv_after_header(NEW_EV, {'样本号', '类别', '细分类别', '标签', '词库原始行号'})
    if h is None:
        info['err'] = '读不到明细表(缺文件或表头不含样本号/类别/细分类别/标签/词库原始行号): %s' % NEW_EV
        return {}, 0, 0, info
    ix = {c: i for i, c in enumerate(h)}

    gold_units = defaultdict(list)
    orphan = amb = 0
    for r in rows:
        p = r[ix['样本号']].strip()
        s = p2s.get(p)
        if not s:
            info['no_row'] += 1
            continue
        key = (r[ix['类别']].strip(), r[ix['细分类别']].strip(), r[ix['标签']].strip())
        nums = [int(n) - 1 for n in re.findall(r'\d+', r[ix['词库原始行号']])]
        if nums:
            u = set(nums)
        else:
            cand = byname.get(key, [])
            if not cand:
                orphan += 1
                continue
            u = set(cand)
            amb += 1
        gold_units[s].append(u)

    info['samples'] = len(gold_units)
    info['rows'] = sum(len(v) for v in gold_units.values())
    return gold_units, orphan, amb, info


def resolve_output_dir(outdir='', raw_dir=''):
    """评测产物目录：显式 --outdir 优先，其次跟随 --raw-dir，最后回到 out。"""
    if outdir:
        return os.path.abspath(outdir)
    if raw_dir:
        return os.path.abspath(raw_dir)
    return os.path.join(ROOT, 'out')


def load_probs(scheme, mode):
    """解析 raw -> {sample: {idx: p}} + 每样本 meta。

    多阶段记录按文件顺序合并：H 的叶子覆盖/补充类别阶段（cat_*不进入标签概率），
    Speculative 的 pass2 覆盖同 qid 的 pass1 概率。带 run_id 的新记录只取
    每样本/阶段最新批次，防止上一轮已问、这一轮未选的标签残留。
    """
    probs = defaultdict(dict)
    meta_raw = {}
    if not os.path.isdir(D_R):
        return probs, meta_raw
    records = []
    for fn in sorted(os.listdir(D_R)):
        nm = parse_name(fn)
        if not nm:
            continue
        s, sc, md = nm
        if scheme and sc != scheme:
            continue
        if mode and md != mode:
            continue
        with open(os.path.join(D_R, fn), encoding='utf-8') as _f:
            for ln in _f:
                if ln.strip():
                    records.append(json.loads(ln))

    latest_run = {}
    for rec in records:
        rid = rec.get('run_id')
        if not rid:
            continue
        group = (rec.get('sample'), rec.get('scheme'), rec.get('mode'), rec.get('stage', 'leaf'))
        if group not in latest_run or str(rec.get('ts', '')) >= latest_run[group][0]:
            latest_run[group] = (str(rec.get('ts', '')), rid)

    for rec in records:
        if rec.get('http') != 200:
            continue
        group = (rec.get('sample'), rec.get('scheme'), rec.get('mode'), rec.get('stage', 'leaf'))
        if group in latest_run and rec.get('run_id') != latest_run[group][1]:
            continue
        smp = rec.get('sample')
        body = rec.get('body') or {}
        probs.setdefault(smp, {})  # 动态筛选后0问也要保留样本，FN不能被静默排除
        m = meta_raw.setdefault(smp, {'model': body.get('model', ''), 'in_tok': 0,
                                      'lat': [], 'api': rec.get('base', '')})
        m['in_tok'] += (body.get('usage') or {}).get('input_tokens', 0)
        if not rec.get('skipped'):
            m['lat'].append(rec.get('latency_ms', 0))
        for qid, ans in (body.get('answers') or {}).items():
            q = re.match(r'q(\d+)$', qid)
            if not q:
                continue
            value = None
            if isinstance(ans, dict):
                for k in ('noul', 'probability', 'prob'):
                    if k in ans:
                        value = ans[k]
                        break
            if isinstance(value, (int, float)):
                probs[smp][int(q.group(1))] = float(value)
    return probs, meta_raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scheme', default='', help='方案名, 对应文件名 {sample}__{scheme}__{mode}.jsonl; 空=全部')
    ap.add_argument('--mode', default='',
                    help='问法粒度; 空=全部。取值 A1|A2|cascade|similarity|hybrid_rag|hybrid|speculative'
                         ' (B 方案是 scheme=B mode=A2)。与 --scheme 是两个维度, 详见 core/README.md §九')
    ap.add_argument('--tau', type=float, default=None,
                    help='命中阈值; 不填则读 tools/raw_to_csv.py 的 DEFAULT_THRESHOLD(%.2f)' % _TAU_DEFAULT)
    ap.add_argument('--tau-lo', type=float, default=None, help='灰区下限, 默认 tau*0.6')
    ap.add_argument('--tau-hi', type=float, default=None, help='灰区上限, 默认 tau')
    ap.add_argument('--api-tag', default='')
    ap.add_argument('--raw-dir', default='', help='原始响应目录; 空=out\\raw')
    ap.add_argument('--payload-dir', default='', help='请求体目录(取 text/meta 用); 空=out\\payloads')
    ap.add_argument('--outdir', default='',
                    help='评测报告和两张结果CSV的输出目录; 空=跟随--raw-dir, 两者都空则out')
    ap.add_argument('--gold', default='old',
                    help='金标准: old=原始资料\\金标准-细分-v1-LLM初稿.csv(默认) / '
                         'new=data\\验证正确数据_v0.2.1(人工答案) / 或直接给 CSV 路径')
    ap.add_argument('--gold-scope', default='run', choices=['run', 'all'],
                    help='金标准统计范围: run=只算本次跑到的样本(默认, 小批量测不会被没跑的样本拖成 FN) / '
                         'all=整份金标准全算(旧行为)')
    ap.add_argument('--alias', action='store_true',
                    help='同名标签归并: 词库里「紧致」等 51 个名字挂了多行, 命中任一行都算对(不展开 FN 分母)')
    ap.add_argument('--tag', default='', help='输出文件名额外后缀, 便于同方案多轮并存')
    ap.add_argument('--overwrite', action='store_true',
                    help='允许覆盖同名输出; 默认遇到已存在文件自动加 _rHHMMSS 后缀')
    # 清单决定 S号 -> CSV记录序号 的映射。换批(1000条)必须同步换清单,
    # 否则 S101 之后在旧清单里查不到, 全部落进 no_row 被静默丢弃。
    ap.add_argument('--list', default='',
                    help='抽样清单路径; 空=data\\抽样清单.csv(100条)。跑1000条要传 data\\抽样清单_1000.csv')
    a = ap.parse_args()

    global D_P, D_R, D_O, SAMPLE_LIST
    if a.list:
        if not os.path.exists(a.list):
            print('!! 抽样清单不存在: %s' % a.list)
            return 1
        SAMPLE_LIST = os.path.abspath(a.list)
        print('抽样清单: %s' % SAMPLE_LIST)
    if a.raw_dir:
        D_R = os.path.abspath(a.raw_dir)
    if a.payload_dir:
        D_P = os.path.abspath(a.payload_dir)
    # 默认把评测产物放在对应 raw 目录，避免所有方案报告堆在 out\ 根目录。
    D_O = resolve_output_dir(a.outdir, a.raw_dir)
    os.makedirs(D_O, exist_ok=True)
    print('评测输出目录: %s' % D_O)

    tau = _TAU_DEFAULT if a.tau is None else a.tau
    tau_lo = round(tau * 0.6, 3) if a.tau_lo is None else a.tau_lo
    tau_hi = tau if a.tau_hi is None else a.tau_hi

    lex = json.load(open(LEXF, encoding='utf-8'))
    labels = {L['idx']: L for L in lex['labels']}
    qgroups = {G['idx']: G for G in merge_label_rows(lex['labels'])}

    # ---------- 金标准 ----------
    tid = {}
    gold_src, gold_key, ginfo = '', 'old', {}
    if a.gold == 'old':
        gold_units, gold_orphan, gold_amb = load_gold(lex)
        gold_src, gold_key = 'LLM 初稿(旧, 未经人审)', 'old'
        for r in csv.DictReader(open(GOLD_SUB, encoding='utf-8-sig')):
            v = (r['词库原始行号'] or '').strip()
            nums = [int(n) - 1 for n in re.findall(r'\d+', v)]
            if len(nums) == 1 and r.get('标签ID'):
                tid[nums[0]] = r['标签ID']
    elif a.gold == 'new':
        gold_units, gold_orphan, gold_amb, ginfo = load_gold_new(lex)
        gold_src, gold_key = '人工答案 v0.2.1', 'new'
        if ginfo.get('err'):
            print('!! %s' % ginfo['err'])
            return 1
    else:
        p = os.path.abspath(a.gold)
        if not os.path.exists(p):
            print('!! 金标准文件不存在: %s' % p)
            return 1
        gold_units, gold_orphan, gold_amb = load_gold(lex, p)
        gold_src = os.path.basename(p)
        gold_key = re.sub(r'[^0-9A-Za-z]+', '_', os.path.splitext(os.path.basename(p))[0])[:18] or 'custom'

    probs, meta_raw = load_probs(a.scheme, a.mode)
    if not probs:
        print('!! scheme=%s mode=%s 没有可用的 200 响应' % (a.scheme or '*', a.mode or '*'))
        print('   文件名形如 out/raw/S001__B__A2.jsonl; --scheme 填 B, --mode 填 A2')
        return 1
    if not gold_units:
        print('!! 对照答案为空: %s' % (GOLD_SUB if gold_key == 'old' else gold_src))
        return 1

    # ---------- 对齐体检(新答案专有): 让「映射到几个样本/有没有撞车」一眼可见 ----------
    if ginfo:
        print('金标准: %s' % gold_src)
        print('  抽样清单 S号 -> P号 : %d 个成功映射%s'
              % (ginfo['mapped'], ('  !! 撞车 %d 处: %s' % (len(ginfo['clash']), ginfo['clash'][:3]))
                 if ginfo['clash'] else ''))
        print('  答案明细落到本批    : %d 行 / %d 个帖 (其余 %d 行属别的样本, 已忽略)'
              % (ginfo['rows'], ginfo['samples'], ginfo['no_row']))
        if ginfo['clash'] or ginfo['dup_s']:
            print('!! 映射有重复, 请先跑 python tools\\check_align.py 排查')
            return 1
        miss = [s for s in probs if s not in gold_units]
        if miss:
            print('  ! 有 %d 个跑到的样本在答案里没有标签: %s' % (len(miss), miss[:6]))

    tag = ((a.scheme or 'ALL') + '_' + (a.mode or 'ALL')) if (a.scheme or a.mode) else 'ALL'
    if gold_key != 'old':
        tag += '_' + gold_key
    if a.alias:
        tag += '_alias'
    if a.tag:
        tag += '__' + a.tag
    # 同名文件已存在 -> 自动加时间戳, 杜绝「跑 3 条把 100 条结果覆盖掉」
    if not a.overwrite and os.path.exists(os.path.join(D_O, '评测报告_%s.md' % tag)):
        import time
        tag += '_r' + time.strftime('%H%M%S')
        print('! 同名报告已存在, 本次输出改名为 %s (要覆盖请加 --overwrite)' % tag)

    # ---------- 预测两表 ----------
    SUB_COLS = ['样本号', '平台', '词库原始行号', '标签ID', '类别', '细分类别', '标签', '评价/提及对象',
                '表达类型', '情绪', '证据字段', '原文证据', 'Jev概率', '命中阈值', '情绪来源', '模型版本', 'API来源']
    SUM_COLS = ['样本号', '平台', '帖子ID（文本）', 'CSV记录序号', '发布时间', '文本字符数', '入样原因',
                '部分标签', '标签证据数', '灰区待复核数', '截断标记', '原帖链接']
    f_sub = open(os.path.join(D_O, '打标结果细分-Jev_%s.csv' % tag), 'w', encoding='utf-8-sig', newline='')
    f_sum = open(os.path.join(D_O, '打标结果汇总-Jev_%s.csv' % tag), 'w', encoding='utf-8-sig', newline='')
    ws, wu = csv.writer(f_sub), csv.writer(f_sum)
    ws.writerow(SUB_COLS); wu.writerow(SUM_COLS)

    lat_all = []
    tok_by_sample = {}
    emo_src = defaultdict(int)
    gray_n = 0
    # 标签级统计: {(sample, idx)}
    pred_pairs = set()
    dim = defaultdict(lambda: [0, 0, 0])          # cat -> [TP, FP, FN]
    fp_list = defaultdict(int)
    fn_list = defaultdict(int)
    fn_not_asked = 0

    for s in sorted(probs):
        tp_txt = os.path.join(D_P, s + '.text.txt')
        mp = os.path.join(D_P, s + '.meta.json')
        if not (os.path.exists(tp_txt) and os.path.exists(mp)):
            print('!! 缺 %s.text.txt 或 .meta.json, 跳过' % s)
            continue
        txt = open(tp_txt, encoding='utf-8').read()
        meta = json.load(open(mp, encoding='utf-8'))
        segs = split_segments(txt)
        P = probs[s]
        hits = sorted([i for i, p in P.items() if p >= tau])
        gray = [i for i in P if tau_lo <= P[i] <= tau_hi and P[i] < tau]
        gray_n += len(gray)
        mr = meta_raw.get(s, {})
        api = a.api_tag or ('官方' if 'typesafe.ai' in str(mr.get('api', ''))
                            else '中转:' + re.sub(r'https?://', '', str(mr.get('api', '?'))))
        lat_all += mr.get('lat', [])
        tok_by_sample[s] = mr.get('in_tok', 0)

        for i in hits:
            L = qgroups.get(i) or labels.get(i)
            if not L:
                continue
            pred_pairs.add((s, i))
            qid = 'q%04d' % i
            direct_info = (meta.get('hybrid_rag', {}) or {}).get('direct_labels', {}).get(qid)
            row_model, row_api = mr.get('model', ''), api
            if direct_info:
                # 强正则证据必须使用“未否定的那个精确命中位置”，不能回搜到另一个否定位置。
                pos = direct_info.get('position')
                kw = L['label']
                segname = next((name for name, start, end in segs
                                if pos is not None and start <= pos < end), None)
                emo, esrc = '不适用', '强正则直接命中'
                ev_f = FIELD_MAP.get(segname, '')
                ev_t = re.sub(r'\s+', ' ', direct_info.get('evidence') or '').strip()
                row_model, row_api = 'local-strong-regex', '本地强正则'
            else:
                segname, pos, kw = find_evidence(txt, segs, L['hits'])
                if pos is None:
                    emo, esrc, ev_f, ev_t = '不适用', '未定位', '语义命中未定位', ''
                else:
                    emo = sentiment(txt, pos, kw, L)
                    esrc = '窗口定位'
                    ev_f = FIELD_MAP.get(segname, '')
                    ev_t = re.sub(r'\s+', ' ', txt[max(0, pos - 30): pos + len(kw) + 30]).strip()
            emo_src[esrc] += 1
            ws.writerow([s, meta.get('site_name', ''), i + 1, tid.get(i, ''), L['cat'], L['sub'], L['label'],
                         '未提取', '未提取', emo, ev_f, ev_t, '%.3f' % P[i], tau, esrc,
                         row_model, row_api])
        names = [ (qgroups.get(i) or labels[i])['label'] for i in hits if (i in qgroups or i in labels)]
        part = '、'.join(names[:8]) + ('；另%d项见明细' % (len(names) - 8) if len(names) > 8 else '')
        wu.writerow([s, meta.get('site_name', ''), meta.get('id', ''), meta.get('csv_row_no', ''),
                     meta.get('publish_time', ''), meta.get('char_count_raw', ''), meta.get('reason', ''),
                     part, len(hits), len(gray), '是' if meta.get('truncated') else '否', meta.get('url', '')])
    f_sub.close(); f_sum.close()

    # ---------- 标签级 TP/FP/FN ----------
    # 只统计本次真正跑到的样本。否则小批量测时, 金标准里"没跑的样本"全被算成 FN,
    # 召回率被稀释成 2% 这种毫无意义的值(实测 3 条时 FN=1768, 全部是没跑的)。
    gold_units_s = {s: u for s, u in gold_units.items() if (a.gold_scope == 'all' or s in probs)}

    gold_raw = set()
    for s, units in gold_units_s.items():
        for u in units:
            for i in u:
                gold_raw.add((s, i))

    # 同名标签归并: 「紧致」在 功效/紧致 和 效果表现/抗老/紧致 各挂一行, 人工标了前者、
    # 模型判了后者, 原样比会算成 1 TP + 1 FP, 双输。--alias 让同名的 idx 互为可接受。
    if a.alias:
        byname_idx = defaultdict(set)
        for L in lex['labels']:
            byname_idx[L['label']].add(L['idx'])
        gold_pairs = set()
        for s, i in gold_raw:
            L = labels.get(i)
            gold_pairs |= {(s, j) for j in byname_idx.get(L['label'], {i})} if L else {(s, i)}
    else:
        gold_pairs = set(gold_raw)

    tp = len(pred_pairs & gold_pairs)
    fp = len(pred_pairs - gold_pairs)
    fn = len(gold_raw - pred_pairs)      # FN 分母始终用未展开的金标准, 避免"改分母抬分"

    for s, i in (pred_pairs - gold_pairs):
        L = labels.get(i)
        if not L:
            continue
        dim[L['cat']][1] += 1
        fp_list['%s / %s' % (L['cat'], L['label'])] += 1
    for s, i in (pred_pairs & gold_pairs):
        L = labels.get(i)
        if L:
            dim[L['cat']][0] += 1
    for s, i in sorted(gold_raw - pred_pairs):
        L = labels.get(i)
        if not L:
            continue
        dim[L['cat']][2] += 1
        asked = i in probs.get(s, {})
        if not asked:
            fn_not_asked += 1
        tag_ = '' if asked else '  [未送问]'
        fn_list['%s / %s%s' % (L['cat'], L['label'], tag_)] += 1

    # unit 级召回(辅助参考): 一个金标准行里任一候选被命中即算覆盖
    covered_units = 0
    total_units = 0
    for s, units in gold_units_s.items():
        H = {i for i, p in probs.get(s, {}).items() if p >= tau}
        for u in units:
            total_units += 1
            if u & H:
                covered_units += 1

    # ---------- 报告 ----------
    pr = tp / (tp + fp) if tp + fp else 0
    rc = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0
    lat = sorted(lat_all)
    n_emo = sum(emo_src.values()) or 1
    rep = []
    rep.append('# Jev 方案④ 评测报告 (scheme=%s, mode=%s, tau=%.2f)\n' % (a.scheme or '*', a.mode or '*', tau))
    if gold_key == 'old':
        rep.append('> ⚠️ 对齐对象是 **LLM 初稿未经人审**(方案 §11-G7), 本报告数字只用于口径开发与冒烟, 不能写入 95% 结论。\n')
    else:
        rep.append('> ✅ 对齐对象是 **%s**。样本对应关系经 `tools\\check_align.py` 验证:'
                   ' 同一 CSV记录序号下 v3.id == 答案帖子ID(10,486 条全等),'
                   ' 答案的 1,441 条「原文证据」有 98.6%% 能在对应正文里逐字搜到。\n' % gold_src)
    rep.append('| 口径项 | 取值 |\n| --- | --- |')
    # 措辞刻意避开「金标准」三个字: 磁盘上真有个文件叫 原始资料\金标准-细分-v1-LLM初稿.csv,
    # 报告里再写「金标准」, 会让人以为永远在对照那份 LLM 初稿, 而 --gold new 读的是人工答案。
    rep.append('| 对照答案(ground truth) | %s |' % gold_src)
    rep.append('| 统计范围 | %s |' % ('只算本次跑到的样本' if a.gold_scope == 'run' else '整份金标准全算'))
    rep.append('| 同名标签归并 | %s |' % ('开(命中任一同名 idx 即算对)' if a.alias else '关'))
    rep.append('')
    rep.append('## 核心指标(标签级: 按 (样本号, 标签) 唯一对计)\n')
    rep.append('| 指标 | 值 |\n| --- | --- |')
    rep.append('| 样本数 | %d |' % len(probs))
    # 指标名跟着口径走, 而不是笼统写「金标准」——否则 1805(LLM初稿) 和 1049(人工答案)
    # 两个数在不同报告里都叫「金标准标签数」, 对不上时根本看不出是换了答案。
    _gold_nm = {'old': 'LLM初稿标签数', 'new': 'LLM模型初步版本标签数'}.get(gold_key, '自定义答案标签数')
    rep.append('| %s | %d |' % (_gold_nm, len(gold_pairs)))
    rep.append('| 预测标签数 | %d |' % len(pred_pairs))
    rep.append('| 命中(TP) | %d |' % tp)
    rep.append('| 标错(FP) | %d |' % fp)
    rep.append('| 漏标(FN) | %d |' % fn)
    rep.append('| **准确率 P** | **%.1f%%** |' % (100 * pr))
    rep.append('| **召回率 R** | **%.1f%%** |' % (100 * rc))
    rep.append('| **F1** | **%.3f** |' % f1)
    rep.append('\n## 成本与稳定性\n')
    rep.append('| 指标 | 值 |\n| --- | --- |')
    rep.append('| 平均输入tok/帖 | %.0f |' % (sum(tok_by_sample.values()) / max(1, len(tok_by_sample))))
    rep.append('| 延迟 ms (p50/p95) | %s / %s |' % (
        lat[len(lat) // 2] if lat else '-', lat[min(len(lat) - 1, int(len(lat) * .95))] if lat else '-'))
    rep.append('| 情绪来源=窗口定位 覆盖率 | %.1f%% |' % (100 * emo_src.get('窗口定位', 0) / n_emo))
    rep.append('| 灰区(%.2f~%.2f)待复核 | %d |' % (tau_lo, tau_hi, gray_n))
    rep.append('\n## 口径与漏标结构\n')
    rep.append('| 指标 | 值 | 说明 |\n| --- | --- | --- |')
    rep.append('| 答案行(unit)数 | %d | 同一标签在不同证据片段会拆成多行 |' % total_units)
    rep.append('| 去重后标签数 | %d | 本报告的 P/R/F1 分母 |' % len(gold_pairs))
    rep.append('| unit 覆盖率 | %.1f%% (%d/%d) | 辅助参考, 不与 P 组 F1 |' % (
        100 * covered_units / total_units if total_units else 0, covered_units, total_units))
    rep.append('| 其中 FN=未送问 | %d | 方案粗筛就没把该标签送进模型, 非模型判错 |' % fn_not_asked)
    rep.append('| 答案行靠重名展开 | %d | 无行号, 按三元组匹配 |' % gold_amb)
    rep.append('| 答案孤儿行 | %d | 三元组也查不到, 已剔除 |' % gold_orphan)
    if fn:
        rep.append('\n> FN 中有 **%d/%d (%.0f%%)** 是"粗筛未送问"造成的结构性漏召,'
                   ' 这部分不是 Jev 判错, 是方案前置筛选的天花板。' % (fn_not_asked, fn, 100 * fn_not_asked / fn))
    rep.append('\n## 分维度 P/R/F1\n')
    rep.append('| 维度 | TP | FP | FN | P | R | F1 |')
    rep.append('| --- | --- | --- | --- | --- | --- | --- |')
    for c, (t, f_, n_) in sorted(dim.items(), key=lambda kv: -sum(kv[1])):
        p_ = t / (t + f_) if t + f_ else 0
        r_ = t / (t + n_) if t + n_ else 0
        F_ = 2 * p_ * r_ / (p_ + r_) if p_ + r_ else 0
        rep.append('| %s | %d | %d | %d | %.2f | %.2f | %.2f |' % (c, t, f_, n_, p_, r_, F_))

    def tops(d, title, n=15):
        rep.append('\n## %s (TOP%d)\n' % (title, n))
        rep.append('| 维度 · 标签 | 次数 |')
        rep.append('| --- | --- |')
        for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]:
            rep.append('| %s | %d |' % (k, v))

    tops(fp_list, '疑似误报(FP)')
    tops(fn_list, '疑似漏报(FN)')

    out = os.path.join(D_O, '评测报告_%s.md' % tag)
    open(out, 'w', encoding='utf-8').write('\n'.join(rep) + '\n')
    print('细分/汇总/报告已输出:')
    print(' ', os.path.join(D_O, '打标结果细分-Jev_%s.csv' % tag))
    print(' ', os.path.join(D_O, '打标结果汇总-Jev_%s.csv' % tag))
    print(' ', out)
    print('准确率P=%.1f%%  召回率R=%.1f%%  F1=%.3f   (TP=%d FP=%d FN=%d, tau=%.2f)'
          % (100 * pr, 100 * rc, f1, tp, fp, fn, tau))
    print('其中 FN 未送问 %d 个 (%.0f%%); unit 覆盖率 %.1f%%'
          % (fn_not_asked, 100 * fn_not_asked / fn if fn else 0,
             100 * covered_units / total_units if total_units else 0))

    # 生成日志：把"这份报告是哪个方案、什么阈值、什么成绩"钉住，避免以后分不清
    try:
        import genlog
        genlog.record('core/jev_eval.py', '生成评测报告', out, 'file', len(rep),
                      note='P/R/F1 + 分维度 + FP/FN TOP（FN 带「未送问」标记）',
                      params={'方案': a.scheme or '全部', '问法': a.mode or '全部',
                              '金标准': gold_src, '范围': a.gold_scope,
                              '同名归并': bool(a.alias),
                              'tau': tau, 'TP': tp, 'FP': fp, 'FN': fn,
                              'P': '%.1f%%' % (100 * pr), 'R': '%.1f%%' % (100 * rc),
                              'F1': '%.3f' % f1})
        for pth, what in ((os.path.join(D_O, '打标结果细分-Jev_%s.csv' % tag), '逐标签预测明细'),
                          (os.path.join(D_O, '打标结果汇总-Jev_%s.csv' % tag), '每帖一行预测汇总')):
            genlog.record('core/jev_eval.py', '导出评测 CSV', pth, 'file', None,
                          note=what, params={'方案': a.scheme or '全部', '问法': a.mode or '全部'})
    except Exception as _e:
        print('!! 生成日志写入失败(不影响结果): %s' % _e)


if __name__ == '__main__':
    sys.exit(main() or 0)
