# -*- coding: utf-8 -*-
"""把 out/raw/*.jsonl 的原始响应整理成可读 CSV。

输出三份：
  out/打标明细.csv        —— 只含**命中**的标签，一行一个（样本 × 标签），交付用
  out/打标概览.csv        —— 一行一个请求，含 http/耗时/token/命中数/命中 TOPIC ANGLE 摘要
  out/打标明细_全量.csv    —— 含未命中的全部问答，评测（算召回/精确率）与灰区复核用

用法：
  python tools/raw_to_csv.py                      # 转换 out/raw 下全部 jsonl
  python tools/raw_to_csv.py --raw out/raw/S001__B__A2.jsonl
  python tools/raw_to_csv.py --threshold 0.5      # 自定义命中阈值（默认 0.6）
  python tools/raw_to_csv.py --top-n 5            # 概览表每帖展示几个 TOPIC ANGLE（默认 8）
"""
import argparse
import csv
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, 'out', 'raw')
PAYLOAD_DIR = os.path.join(ROOT, 'out', 'payloads')
LABEL_FILE = os.path.join(ROOT, 'data', '标签体系_Jev整理版.csv')

# 唯一命中阈值默认值：jev_client.py 也读这个常量，避免两个入口默认值不一致
# 0.6 = 只有概率 >= 0.6 才算命中（比 0.5 更严，宁少不多）
DEFAULT_THRESHOLD = 0.6


def load_labels():
    """标签问题ID -> {类别, 细分类别, TOPIC ANGLE, 标签层级}"""
    m = {}
    if not os.path.exists(LABEL_FILE):
        return m
    with open(LABEL_FILE, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            qid = (row.get('标签问题ID') or '').strip()
            if not qid:
                continue
            m[qid] = {
                '类别': (row.get('类别') or '').strip(),
                '细分类别': (row.get('细分类别') or '').strip(),
                'TOPIC ANGLE': (row.get('TOPIC ANGLE') or '').strip(),
                '标签层级': (row.get('标签层级') or '').strip(),
            }
    return m


def load_payload_questions(payload_dir=None):
    """(sample, scheme, mode) -> {qid: question dict}，用来取问题原文"""
    idx = {}
    for fp in glob.glob(os.path.join(payload_dir or PAYLOAD_DIR, '*.jsonl')):
        name = os.path.basename(fp)[:-6]
        parts = name.split('__')
        if len(parts) == 3:
            key = (parts[0], parts[1], parts[2], 'leaf')
        elif len(parts) == 4:
            # 多阶段：{样本}__{方案}__{模式}__{阶段}.jsonl
            key = (parts[0], parts[1], parts[2], parts[3])
        elif len(parts) == 1:
            key = (parts[0], '', '', 'leaf')
        else:
            continue
        qs = {}
        with open(fp, encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                p = json.loads(line)
                qs.update(p.get('questions') or {})
        if qs:
            idx[key] = qs
    return idx


def parse_answer(ans):
    """统一解析 choice / noul / score 三种原语"""
    if not isinstance(ans, dict):
        return {'type': '', 'value': '', 'confidence': '', 'hit': ''}
    t = ans.get('type', '')
    if t == 'choice':
        return {'type': t, 'value': ans.get('choice', ''), 'confidence': ans.get('confidence', ''),
                'hit': ans.get('choice', ''), 'probs': ans.get('probabilities') or {}}
    if t == 'noul':
        v = ans.get('noul', '')
        return {'type': t, 'value': v, 'confidence': '', 'hit': v, 'probs': {}}
    if t == 'score':
        return {'type': t, 'value': ans.get('score', ''), 'confidence': ans.get('confidence', ''),
                'hit': ans.get('score', ''), 'probs': {}}
    return {'type': t, 'value': json.dumps(ans, ensure_ascii=False), 'confidence': '', 'hit': '', 'probs': {}}


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def write_csv(path, rows):
    """写 CSV；若目标文件被 Excel 占用则改写到带时间戳的备用文件，返回实际路径。"""
    def _write(p):
        with open(p, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    try:
        _write(path)
        return path
    except PermissionError:
        import time as _t
        base, ext = os.path.splitext(path)
        alt = '%s_%s%s' % (base, _t.strftime('%H%M%S'), ext)
        _write(alt)
        print('!! %s 被占用（多半是 Excel 开着），已改写到: %s' % (os.path.basename(path), os.path.basename(alt)))
        return alt


def export(raw=None, threshold=None, outdir=None, quiet=False, top_n=8,
           raw_dir=None, payload_dir=None):
    """把 out/raw/*.jsonl 导出为明细/概览两份 CSV。

    raw:        指定单个 jsonl 路径；None 表示处理 raw_dir 下全部
    threshold:  noul 命中阈值；None 表示用 DEFAULT_THRESHOLD
    outdir:     输出目录，默认 <项目根>/out
    top_n:      概览表里每个帖子最多展示几个命中 TOPIC ANGLE（默认 8，按概率从高到低）
    raw_dir:    原始响应目录，默认 out/raw。多方案并行时传各自目录，避免混合
    payload_dir: 请求体目录，默认 out/payloads。用来取问题原文
    返回 (明细路径, 概览路径, 明细行数, 概览行数)；无数据时返回 (None, None, 0, 0)
    """
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    outdir = outdir or os.path.join(ROOT, 'out')
    src = raw_dir or RAW_DIR
    files = [raw] if raw else sorted(glob.glob(os.path.join(src, '*.jsonl')))
    if not files:
        if not quiet:
            print('未找到 %s\\*.jsonl，请先跑一次 jev_client.py' % src)
        return None, None, 0, 0

    labels = load_labels()
    pqs = load_payload_questions(payload_dir)

    # 同一 (样本, 方案, 模式, 阶段, 分块) 若被重跑多次，只保留最后一条。
    # 新客户端带 run_id：每样本/阶段只取最新完整批次，避免上一轮候选残留。
    all_records = []
    for fp in files:
        with open(fp, encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    all_records.append(json.loads(line))
    latest_run = {}
    for r in all_records:
        rid = r.get('run_id')
        if not rid:
            continue
        group = (r.get('sample'), r.get('scheme'), r.get('mode'), r.get('stage', 'leaf'))
        if group not in latest_run or str(r.get('ts', '')) >= latest_run[group][0]:
            latest_run[group] = (str(r.get('ts', '')), rid)
    latest = {}
    for r in all_records:
        group = (r.get('sample'), r.get('scheme'), r.get('mode'), r.get('stage', 'leaf'))
        if group in latest_run and r.get('run_id') != latest_run[group][1]:
            continue
        key = group + (r.get('chunk'),)
        latest[key] = r
    records = sorted(latest.values(), key=lambda r: (str(r.get('sample')), str(r.get('scheme')),
                                                       str(r.get('stage', 'leaf')), r.get('chunk') or 0))

    # 多阶段交付结果合并：H 只保留叶子 qid（类别仅作门控），Speculative 的
    # pass2 覆盖 pass1；usage/耗时仍合计所有阶段。raw JSON 继续保留原始逐阶段记录。
    grouped = {}
    for r in records:
        key = (r.get('sample'), r.get('scheme'), r.get('mode'))
        grouped.setdefault(key, []).append(r)
    merged_records = []
    for (sample, scheme, mode), group in grouped.items():
        if scheme not in ('H', 'Speculative'):
            merged_records.extend(group)
            continue
        stage_rank = {'gate_cat': 0, 'pass1': 0, 'leaf': 1, 'pass2': 1}
        group.sort(key=lambda r: (stage_rank.get(r.get('stage', 'leaf'), 2), r.get('chunk') or 0))
        answers = {}
        answer_stages = {}
        usage = {'input_tokens': 0, 'output_tokens': 0}
        latency = 0
        for r in group:
            body = r.get('body') if isinstance(r.get('body'), dict) else {}
            for qid, answer in (body.get('answers') or {}).items():
                if qid.startswith('q') and qid[1:].isdigit():
                    answers[qid] = answer
                    answer_stages[qid] = r.get('stage', 'leaf')
            u = body.get('usage') or {}
            usage['input_tokens'] += int(u.get('input_tokens') or 0)
            usage['output_tokens'] += int(u.get('output_tokens') or 0)
            latency += int(r.get('latency_ms') or 0)
        last = dict(group[-1])
        body = dict(last.get('body') or {})
        body['answers'] = answers
        body['usage'] = usage
        last['body'] = body
        last['latency_ms'] = latency
        last['source_stages'] = list(dict.fromkeys(r.get('stage', 'leaf') for r in group))
        last['answer_stages'] = answer_stages
        last['stage'] = 'final'
        last['chunk'] = 1
        last['nchunks'] = 1
        last['skipped'] = all(r.get('skipped', False) for r in group)
        merged_records.append(last)
    records = merged_records

    detail, full, overview = [], [], []
    for r in records:
        body = r.get('body') if isinstance(r.get('body'), dict) else {}
        ans = body.get('answers') or {}
        usage = body.get('usage') or {}
        qmap = pqs.get((r.get('sample'), r.get('scheme'), r.get('mode'), r.get('stage', 'leaf')), {})

        hits = 0
        angle_prob = {}   # TOPIC ANGLE -> 最高命中概率（同名标签去重）
        for qid, raw_ans in ans.items():
            p = parse_answer(raw_ans)
            lab = labels.get(qid, {})
            num = to_num(p['value'])
            if p['type'] == 'noul':
                is_hit = num is not None and num >= threshold
                hit_flag = 'Y' if is_hit else 'N'
                hits += 1 if is_hit else 0
                # 灰区：概率贴近阈值、判定不稳，建议人工复核
                gray = 'Y' if (num is not None and threshold * 0.6 <= num < threshold) else ''
            elif p['type'] == 'choice':
                is_hit = bool(p['value'])
                hit_flag = p['value']
                gray = ''
            else:
                is_hit = False
                hit_flag = ''
                gray = ''
            if is_hit:
                # 概览用的 TOPIC ANGLE 摘要：同名标签只留最高概率
                name = lab.get('TOPIC ANGLE', '') or lab.get('细分类别', '')
                if name:
                    pv = num if num is not None else 1.0
                    if pv > angle_prob.get(name, -1.0):
                        angle_prob[name] = pv
            if r.get('stage') == 'final':
                answer_stage = (r.get('answer_stages') or {}).get(qid, 'leaf')
                stage_qmap = pqs.get((r.get('sample'), r.get('scheme'), r.get('mode'), answer_stage), {})
                qdef = stage_qmap.get(qid) or {}
            else:
                qdef = qmap.get(qid) or {}
            row = {
                '样本': r.get('sample', ''),
                '方案': r.get('scheme', ''),
                '阶段': r.get('stage', ''),
                '问题ID': qid,
                '类别': lab.get('类别', ''),
                '细分类别': lab.get('细分类别', ''),
                'TOPIC ANGLE': lab.get('TOPIC ANGLE', ''),
                '标签层级': lab.get('标签层级', ''),
                '问题类型': p['type'],
                '概率/取值': p['value'],
                '置信度': p['confidence'],
                '问题原文': (qdef.get('instructions') or '').replace('\n', ' '),
                '模型': body.get('model', ''),
                '时间': r.get('ts', ''),
            }
            full_row = dict(row)
            full_row['是否命中'] = hit_flag
            full_row['灰区'] = gray
            full_row['HTTP'] = r.get('http', '')
            full_row['耗时ms'] = r.get('latency_ms', '')
            full_row['input_tokens'] = usage.get('input_tokens', '')
            full_row['output_tokens'] = usage.get('output_tokens', '')
            full.append(full_row)
            if is_hit:
                detail.append(row)

        # 按概率从高到低取前 top_n 个 TOPIC ANGLE
        top_angles = sorted(angle_prob.items(), key=lambda kv: -kv[1])[:top_n]
        angle_txt = '、'.join(name for name, _ in top_angles)

        overview.append({
            '样本': r.get('sample', ''),
            '方案': r.get('scheme', ''),
            '阶段': r.get('stage', ''),
            '分块': '%s/%s' % (r.get('chunk', ''), r.get('nchunks', '')),
            'HTTP': r.get('http', ''),
            '耗时ms': r.get('latency_ms', ''),
            '模型': body.get('model', ''),
            '答案数': len(ans),
            '命中标签数': hits,
            'TOPIC ANGLE(前%d)' % top_n: angle_txt,
            '命中阈值': threshold,
            'input_tokens': usage.get('input_tokens', ''),
            'output_tokens': usage.get('output_tokens', ''),
            '估tokens': r.get('est_tokens', ''),
            '时间': r.get('ts', ''),
        })

    os.makedirs(outdir, exist_ok=True)
    p1 = os.path.join(outdir, '打标明细.csv')        # 仅命中：交付用
    p2 = os.path.join(outdir, '打标概览.csv')        # 每请求一行
    p3 = os.path.join(outdir, '打标明细_全量.csv')    # 含未命中：评测/复核用
    if detail:
        p1 = write_csv(p1, detail)
    if overview:
        p2 = write_csv(p2, overview)
    if full:
        p3 = write_csv(p3, full)

    if not quiet:
        print('明细(仅命中): %s (%d 行)' % (p1, len(detail)))
        print('概览: %s (%d 行)' % (p2, len(overview)))
        print('全量(含未命中): %s (%d 行)' % (p3, len(full)))
        if detail:
            miss = sum(1 for d in detail if not d['类别'])
            print('标签匹配: %d/%d 条匹配到词库' % (len(detail) - miss, len(detail)))
    # 生成日志：每次导出记 3 条（明细/概览/全量），换阈值重导也能看出是哪一版
    try:
        core = os.path.join(ROOT, 'core')
        if core not in sys.path:
            sys.path.insert(0, core)
        import genlog
        for pth, cnt, what in ((p1, len(detail), '仅命中，交付用'),
                               (p2, len(overview), '每帖一行，含 TOPIC ANGLE 前%d' % top_n),
                               (p3, len(full), '含未命中，评测/复核用')):
            genlog.record('tools/raw_to_csv.py', '导出 CSV', pth, 'file', cnt,
                          note=what,
                          params={'阈值': threshold, 'top_n': top_n,
                                  'raw来源': os.path.basename(src.rstrip('\\/'))})
    except Exception as _e:
        print('!! 生成日志写入失败(不影响结果): %s' % _e)

    return p1, p2, len(detail), len(overview)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--raw', default='', help='指定单个 jsonl，默认处理 out/raw 下全部')
    ap.add_argument('--threshold', type=float, default=None,
                    help='noul 命中阈值，默认 %s' % DEFAULT_THRESHOLD)
    ap.add_argument('--top-n', type=int, default=8, help='概览表每帖最多展示几个 TOPIC ANGLE，默认 8')
    ap.add_argument('--outdir', default=os.path.join(ROOT, 'out'))
    ap.add_argument('--raw-dir', default='', help='原始响应目录; 默认 out\\raw')
    ap.add_argument('--payload-dir', default='', help='请求体目录(取问题原文用); 默认 out\\payloads')
    a = ap.parse_args()

    p1, p2, n1, n2 = export(raw=a.raw or None, threshold=a.threshold, outdir=a.outdir,
                            top_n=a.top_n, raw_dir=a.raw_dir or None,
                            payload_dir=a.payload_dir or None)
    return 0 if n1 or n2 else 1


if __name__ == '__main__':
    sys.exit(main() or 0)
