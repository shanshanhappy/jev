# -*- coding: utf-8 -*-
"""genlog.py  生成日志：让目录里每个产物都能说清"我是什么、谁生成的、能不能删"

设计要点：
  - 唯一数据源 `out/生成日志.jsonl`（只追加，永不覆盖，便于回溯）
  - `out/生成日志.md` 由 jsonl 重建（时间倒序），顶部是静态「文件说明书」，
    后面跟「最近记录」和「当前快照」（扫描磁盘，标注来源不明的文件）
  - 记录失败绝不影响主流程：调用方都包在 try 里，本模块内部也吞异常

对外只用到三个函数：
  record(...)  记一笔（脚本产出文件后调用）
  rebuild_md() 重建 md（record 自动调用，也可单独跑）
  snapshot()   扫描 out/ 现状，返回 (行列表)

命令行见 tools/genlog.py
"""
import csv
import glob
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'out')
LOGJ = os.path.join(OUT, '生成日志.jsonl')
LOGM = os.path.join(OUT, '生成日志.md')
MAX_MD_RECORDS = 40          # md 里最多展示多少条记录（全量在 jsonl）
COUNT_LIMIT = 8 * 1024 * 1024  # 超过 8MB 的文件不数行，只报大小

# ---------------------------------------------------------------- 文件说明书
# 每个已知产物的说明。path 支持 * 通配。
CATALOG = [
    # 原始资料
    ('data/label_lexicon.json', '词库结构化版本：638 个概念 + 关键词/排除词',
     'tools/build_lexicon.py', '打标实际读这个；改标签体系后必须重跑并重新生成 payload'),
    ('data/标签体系_Jev整理版.csv', '词库的人读版：645 行，含 TTL 汇总节点',
     'tools/build_taxonomy.py', '只是导出/反查用；改这里不影响打标'),
    ('data/标签体系_原始行映射.csv', '678 原始行 ↔ qid 对照表',
     'tools/build_taxonomy.py', '人工回溯用'),
    ('data/抽样清单.csv', '100 条样本的抽样清单（样本号 ↔ CSV 记录序号）',
     'tools/make_sample_list.py', 'schemes.py 按它找样本'),

    # 请求体
    ('out/payloads', '请求体目录：准备发给 Jev 的内容（生成不花钱）',
     'core/schemes.py', '父目录'),
    ('out/payloads/*__*__*.jsonl', '发给 Jev 的请求 body，一行一个 HTTP 请求（超长帖会切多行）',
     'core/schemes.py', '重跑同名直接覆盖；改粗筛/措辞后必须重新生成'),
    ('out/payloads/*.text.txt', '模型实际看到的正文（已按 --max-chars 截断）',
     'core/schemes.py', '排查漏标时看这个；文件名不含方案名，多方案会互相覆盖'),
    ('out/payloads/*.meta.json', '溯源元信息：原始 CSV 行号、URL、字数、问题数、估算 token',
     'core/schemes.py', '文件名不含方案名，多方案会互相覆盖'),

    # 原始响应
    ('out/raw', 'Jev 原始响应目录：每个请求的完整 body + usage + 耗时',
     'core/jev_client.py', '**追加写入**，重跑不会覆盖，建议改名备份'),
    ('out/raw/*.jsonl', '一次请求一行：http 状态、answers（概率）、usage、耗时',
     'core/jev_client.py', '所有 CSV 和评测都从这里算，是唯一事实来源'),

    # 导出层
    ('out/打标明细.csv', '只含命中标签（noul ≥ 阈值），14 列，交付用',
     'tools/raw_to_csv.py', '0 成本重导：python tools\\raw_to_csv.py'),
    ('out/打标明细_全量.csv', '含未命中标签，20 列，复核/评测用',
     'tools/raw_to_csv.py', '比明细多「是否命中」「灰区」两列'),
    ('out/打标概览.csv', '每帖一行：命中数、TOPIC ANGLE 前 N 个',
     'tools/raw_to_csv.py', '换阈值/改 TOP N 都只需重导，不花 token'),

    # 评测层
    ('out/打标结果细分-Jev_*.csv', '逐标签预测明细（含是否命中金标准）',
     'core/jev_eval.py', 'tag = 方案_问法，如 B_A2'),
    ('out/打标结果汇总-Jev_*.csv', '每帖一行预测汇总',
     'core/jev_eval.py', ''),
    ('out/评测报告_*.md', 'P/R/F1 + 分维度 + FP/FN TOP（含「未送问」标记）',
     'core/jev_eval.py', '改 tau 重跑即可，0 成本'),

    # 金标准
    ('原始资料/金标准-细分-v1-LLM初稿.csv', '参照答案：样本 × 标签 × 对象 × 证据（LLM 初稿，未经人审）',
     'LLM 初稿 + tools/normalize_gold.py 归一', '改它等于改考卷，务必先备份'),
    ('原始资料/金标准-细分-v1-LLM初稿.bak-*.csv', '金标准历次备份（归一化/去重前）',
     'tools/normalize_gold.py', '回滚用，确认无误后可清理'),

    # 日志自身
    ('out/生成日志.md', '本文件：文件说明书 + 当前快照 + 最近记录',
     'core/genlog.py', '自动重建，勿手改'),
    ('out/生成日志.jsonl', '日志数据源（追加），一行一条记录',
     'core/genlog.py', '删了只会丢历史记录，不影响数据'),

    # 其他（与打标选型无直接关系）
    ('out/jev_platform_compare.csv', 'Jev 可获取平台对比表（数据）',
     '人工调研整理', '与打标结果无关，留档用'),
    ('out/jev_platform_compare.md', 'Jev 可获取平台对比表（结论）',
     '人工调研整理', '与打标结果无关，留档用'),
]

# 子目录在快照里只汇总，不逐个列文件
SUMMARIZE_DIRS = ['out/payloads', 'out/raw']


def _rel(p):
    """绝对路径 -> 相对项目根的 / 分隔路径"""
    try:
        p = os.path.relpath(p, ROOT)
    except ValueError:
        pass
    return p.replace('\\', '/')


def _match(relpath, pattern):
    """pattern 里的 * 匹配任意非 / 字符"""
    rx = '^' + re.escape(pattern).replace(r'\*', '[^/]*') + '$'
    return re.match(rx, relpath) is not None


def lookup(relpath):
    """查说明书：返回 (是什么, 谁生成, 备注) 或 None"""
    for path, desc, who, note in CATALOG:
        if _match(relpath, path):
            return desc, who, note
    return None


def count_rows(path):
    """行数（csv/jsonl/md/py）；json/二进制或不认识的返回 None"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > COUNT_LIMIT:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext not in ('.csv', '.jsonl', '.md', '.txt', '.py'):
        return None
    try:
        with open(path, 'rb') as f:
            n = sum(1 for _ in f)
        return n - 1 if ext == '.csv' and n else n   # csv 去掉表头
    except OSError:
        return None


def _fmt_size(n):
    for unit in ('B', 'KB', 'MB'):
        if n < 1024 or unit == 'MB':
            return ('%d%s' % (n, unit)) if unit == 'B' else ('%.1f%s' % (n, unit))
        n /= 1024.0
    return '%dB' % n


def snapshot():
    """扫描 out/ 现状 -> [(显示名, 大小, 行数, 修改时间, 说明, 是否已知)]"""
    rows = []
    if not os.path.isdir(OUT):
        return rows

    # 1) 汇总子目录
    for d in SUMMARIZE_DIRS:
        ap = os.path.join(ROOT, d.replace('/', os.sep))
        if not os.path.isdir(ap):
            continue
        files = [f for f in glob.glob(os.path.join(ap, '*')) if os.path.isfile(f)]
        total = sum(os.path.getsize(f) for f in files)
        latest = max((os.path.getmtime(f) for f in files), default=0)
        info = lookup(d)
        rows.append((d + '/', _fmt_size(total), '%d 个文件' % len(files),
                     time.strftime('%m-%d %H:%M', time.localtime(latest)) if latest else '-',
                     info[0] if info else '未知目录', info is not None))

    # 2) out/ 顶层文件
    for f in sorted(glob.glob(os.path.join(OUT, '*'))):
        if not os.path.isfile(f):
            continue
        rel = _rel(f)
        info = lookup(rel)
        rows.append((_rel(f), _fmt_size(os.path.getsize(f)), count_rows(f),
                     time.strftime('%m-%d %H:%M', time.localtime(os.path.getmtime(f))),
                     info[0] if info else '不在说明书里，可能是临时/手工文件', info is not None))
    return rows


def record(script, action, target, kind='file', rows=None, note='', params=None, quiet=True):
    """记一笔。任何异常都吞掉，绝不影响主流程。"""
    try:
        os.makedirs(OUT, exist_ok=True)
        rec = {
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'script': script,
            'action': action,
            'target': _rel(target) if target else '',
            'kind': kind,
            'rows': rows,
            'note': note,
            'params': {k: v for k, v in (params or {}).items() if v not in (None, '')},
        }
        with open(LOGJ, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        if not quiet:
            print('[日志] %s → %s' % (action, rec['target']))
        rebuild_md()
        return True
    except Exception:
        return False


def load_records():
    if not os.path.exists(LOGJ):
        return []
    out = []
    for ln in open(LOGJ, encoding='utf-8'):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def rebuild_md():
    """把 jsonl 渲染成 out/生成日志.md（时间倒序）"""
    recs = load_records()
    rep = []
    rep.append('# 生成日志')
    rep.append('')
    rep.append('> 自动生成，勿手改。数据源 `out/生成日志.jsonl`（追加写入）。')
    rep.append('> 重建：`python tools\\genlog.py --rebuild`')
    rep.append('')

    # ---- 一、文件说明书
    rep.append('## 一、文件说明书（目录里每个东西是什么）')
    rep.append('')
    rep.append('| 路径 | 是什么 | 谁生成 | 备注 |')
    rep.append('| --- | --- | --- | --- |')
    for path, desc, who, note in CATALOG:
        rep.append('| `%s` | %s | `%s` | %s |' % (path, desc, who, note))
    rep.append('')

    # ---- 二、当前快照
    rep.append('## 二、当前快照（磁盘实况）')
    rep.append('')
    snap = snapshot()
    if snap:
        rep.append('| 文件/目录 | 大小 | 行数 | 修改时间 | 说明 |')
        rep.append('| --- | --- | --- | --- | --- |')
        for name, size, cnt, mt, desc, known in snap:
            mark = '' if known else ' ⚠'
            rep.append('| `%s` | %s | %s | %s | %s%s |'
                       % (name, size, cnt if cnt is not None else '-', mt, desc, mark))
    else:
        rep.append('_out/ 下暂无文件_')
    rep.append('')

    # ---- 三、最近记录
    rep.append('## 三、最近记录（时间倒序，最多 %d 条）' % MAX_MD_RECORDS)
    rep.append('')
    if recs:
        rep.append('| 时间 | 脚本 | 动作 | 产物 | 数量 | 参数/说明 |')
        rep.append('| --- | --- | --- | --- | --- | --- |')
        for r in reversed(recs[-MAX_MD_RECORDS:]):
            extra = []
            p = r.get('params') or {}
            if p:
                extra.append(' '.join('%s=%s' % (k, v) for k, v in p.items()))
            if r.get('note'):
                extra.append(r['note'])
            rep.append('| %s | `%s` | %s | `%s` | %s | %s |'
                       % (r.get('ts', ''), r.get('script', ''), r.get('action', ''),
                          r.get('target', '-'), r.get('rows') if r.get('rows') is not None else '-',
                          '；'.join(extra)))
        if len(recs) > MAX_MD_RECORDS:
            rep.append('')
            rep.append('_更早的 %d 条见 `out/生成日志.jsonl`_' % (len(recs) - MAX_MD_RECORDS))
    else:
        rep.append('_暂无记录。下次跑 schemes / jev_client / raw_to_csv / jev_eval 会自动写入。_')
    rep.append('')

    tmp = LOGM + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(rep))
    os.replace(tmp, LOGM)
    return LOGM


SCRIPTS_SHOULD_LOG = [
    'core/schemes.py', 'core/jev_client.py', 'core/jev_eval.py',
    'tools/raw_to_csv.py', 'tools/normalize_gold.py',
    'tools/build_lexicon.py', 'tools/build_taxonomy.py',
]


def doctor():
    """自检：找出会让日志写不进去、或让产物无法溯源的环境问题"""
    print('== 生成日志自检 ==\n')
    ok = True

    def mods(d):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p):
            return set()
        return {os.path.splitext(f)[0] for f in os.listdir(p) if f.endswith('.py')}

    # 1. 同名模块（core 脚本 import 时可能拿到 tools 版本）
    dup = sorted(mods('core') & mods('tools'))
    if dup:
        ok = False
        print('[X] core\\ 与 tools\\ 有同名模块: %s' % ', '.join(dup))
        print('    影响: core 脚本 import 时可能拿到 tools 版本（曾导致 genlog 写不进）')
        print('    处理: 删除或改名 tools\\ 下的同名文件')
    else:
        print('[OK] 无同名模块冲突')

    # 2. sys.path 顺序（把 tools 插到最前 = 上述冲突被触发的条件）
    bad = []
    for d in ('core', 'tools'):
        for f in glob.glob(os.path.join(ROOT, d, '*.py')):
            try:
                src = open(f, encoding='utf-8').read()
            except OSError:
                continue
            if re.search(r'sys\.path\.insert\(\s*0\s*,[^)]*tools', src):
                bad.append(_rel(f))
    if bad:
        ok = False
        print('[X] 以下文件把 tools 插到 sys.path 最前: %s' % ', '.join(bad))
        print('    改成 sys.path.append(...)（core\\ 已在 sys.path[0]，不需要抢优先）')
    else:
        print('[OK] 没有 insert(0, tools)')

    # 3. 埋点覆盖
    missing = []
    for s in SCRIPTS_SHOULD_LOG:
        p = os.path.join(ROOT, s.replace('/', os.sep))
        if not os.path.exists(p):
            missing.append(s + '(文件不存在)')
            continue
        try:
            if 'genlog.record' not in open(p, encoding='utf-8').read():
                missing.append(s)
        except OSError:
            missing.append(s + '(读不了)')
    if missing:
        ok = False
        print('[X] 缺少埋点: %s' % ', '.join(missing))
    else:
        print('[OK] %d 个脚本都有 genlog.record 埋点' % len(SCRIPTS_SHOULD_LOG))

    # 4. jsonl 可解析
    recs = load_records()
    lines = 0
    if os.path.exists(LOGJ):
        lines = sum(1 for ln in open(LOGJ, encoding='utf-8') if ln.strip())
    if lines != len(recs):
        ok = False
        print('[X] 日志有 %d 行、只能解析 %d 条（存在坏行）' % (lines, len(recs)))
    else:
        print('[OK] 日志可解析: %d 条' % len(recs))

    # 5. md 是否与 jsonl 同步
    if not os.path.exists(LOGM):
        print('[!] 还没有 md，跑一次 python core\\genlog.py --rebuild')
    elif os.path.exists(LOGJ) and os.path.getmtime(LOGM) + 1 < os.path.getmtime(LOGJ):
        print('[!] md 比 jsonl 旧，建议 --rebuild')
    else:
        print('[OK] md 与 jsonl 同步')

    print()
    print('结论: %s' % ('全部通过' if ok else '有需要处理的问题（见上面 [X]）'))
    return 0 if ok else 1


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    # 既能当库被 import，也能直接当命令跑：
    #   python core\genlog.py              看快照 + 最近记录
    #   python core\genlog.py --snapshot   只看磁盘现状
    #   python core\genlog.py --tail 30    最近 30 条
    #   python core\genlog.py --rebuild    重建 md
    #   python core\genlog.py --clear --yes 清空日志（不动数据文件）
    import argparse

    def _show_snapshot():
        rows = snapshot()
        if not rows:
            print('out/ 下暂无文件')
            return
        print('%-44s %9s %10s %12s  %s' % ('文件/目录', '大小', '行数', '修改时间', '说明'))
        print('-' * 140)
        for name, size, cnt, mt, desc, known in rows:
            print('%-44s %9s %10s %12s  %s%s'
                  % (name, size, str(cnt) if cnt is not None else '-', mt, desc,
                     '' if known else ' [来源未知]'))

    def _show_tail(n):
        recs = load_records()
        if not recs:
            print('暂无记录')
            return
        print('最近 %d 条（共 %d 条）：\n' % (min(n, len(recs)), len(recs)))
        for r in reversed(recs[-n:]):
            params = r.get('params') or {}
            print('%s  %-24s %-12s %s%s'
                  % (r.get('ts', ''), r.get('script', ''), r.get('action', ''),
                     r.get('target', '-'),
                     '' if r.get('rows') is None else '  [%s]' % r['rows']))
            if params:
                print('    ' + '  '.join('%s=%s' % (k, v) for k, v in params.items()))
            if r.get('note'):
                print('    ' + r['note'])

    ap = argparse.ArgumentParser('生成日志')
    ap.add_argument('--snapshot', action='store_true', help='只看磁盘现状')
    ap.add_argument('--tail', type=int, default=0, help='显示最近 N 条记录')
    ap.add_argument('--rebuild', action='store_true', help='重建 out/生成日志.md')
    ap.add_argument('--clear', action='store_true', help='清空日志(md+jsonl)')
    ap.add_argument('--doctor', action='store_true', help='自检: 同名模块/路径顺序/埋点/日志完整性')
    ap.add_argument('--yes', action='store_true', help='--clear 时确认')
    a = ap.parse_args()
    if a.doctor:
        raise SystemExit(doctor())
    if a.clear:
        if not a.yes:
            print('要清空日志请加 --yes（只删日志，不动任何数据文件）')
        else:
            for p in (LOGJ, LOGM):
                if os.path.exists(p):
                    os.remove(p)
                    print('已删除 %s' % p)
            rebuild_md()
    elif a.rebuild:
        print('已重建: %s' % rebuild_md())
    elif a.snapshot:
        _show_snapshot()
    elif a.tail:
        _show_tail(a.tail)
    else:
        _show_snapshot()
        print()
        _show_tail(15)
