# -*- coding: utf-8 -*-
r"""jev_prompts.py  第3步: data_export_clean_v3.csv + label_lexicon.json -> Jev 请求体
按 docs\数据处理方案.md §2.4/§10.3: 每个标签一个 Noul 问题, qid = "q" + 词库数据行号。

⚠️ 兼容入口: 日常请用 core\schemes.py（输出带 scheme 字段, 能被 jev_client --scheme 过滤）。
   本文件的产物是 {sample}.jsonl, 无 scheme 字段, --scheme 过滤不到, 只保留做历史复现。

三种模式(§0.2):
  A1 纯语义   问题只含标签名+维度            (最省, ~25k tok/帖)
  A2 语义+提示 问题附词库命中词/排除词        (默认主推, ~60k tok/帖, 自动分块)
  B  词库粗筛  只问"命中关键词字面出现在文本里"的标签 (~3-8k tok/帖)

用法(在项目根目录):
  python core\jev_prompts.py                          # 金标准100样本 x 三模式
  python core\jev_prompts.py --samples S001,S002      # 指定样本
  python core\jev_prompts.py --modes A2 --budget 55000 --max-chars 2000
  python core\jev_prompts.py --modes B --qstyle v2 --excls-scope window

输出:
  out\payloads\{sample}.jsonl      一行=一个可直接 POST /v1/systemone 的请求体
  out\payloads\{sample}.text.txt   实际进模型的全文(回搜证据用)
  out\payloads\{sample}.meta.json  csv_row_no/截断标记/字符数等
"""
import argparse, csv, json, os, re, sys
from jev_rules import merge_label_rows, screen_label, q_instruction, est_tokens

sys.stdout.reconfigure(encoding='utf-8')
csv.field_size_limit(10 * 1024 * 1024)
ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEX   = os.path.join(ROOT, 'data', 'label_lexicon.json')
V3    = os.path.join(ROOT, 'data', 'data_export_clean_v3.csv')
GOLD  = os.path.join(ROOT, 'data', '抽样清单.csv')
OUTD  = os.path.join(ROOT, 'out', 'payloads')


def build_chunks(state, questions, budget):
    """questions 按 est_tokens 预算切块; 每个 chunk 都要带 state, 故先扣掉 state 的 token"""
    state_t = est_tokens(json.dumps(state, ensure_ascii=False))
    avail = max(2000, budget - state_t)
    chunks, cur, cur_t = [], {}, 0
    for qid, q in questions.items():
        t = est_tokens(json.dumps({qid: q}, ensure_ascii=False))
        if cur and cur_t + t > avail:
            chunks.append(cur); cur, cur_t = {}, 0
        cur[qid] = q; cur_t += t
    if cur:
        chunks.append(cur)
    return chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--samples', default='', help='逗号分隔样本号; 默认取清单全部')
    ap.add_argument('--gold', default=GOLD, help='抽样清单CSV(样本号/CSV记录序号/入样原因)')
    ap.add_argument('--modes', default='A1,A2,B')
    ap.add_argument('--max-chars', type=int, default=2000, help='进模型的全文截断上限(§11-G5)')
    ap.add_argument('--budget', type=int, default=55000, help='单请求总预算(含 state, 留余量<64k)')
    ap.add_argument('--model', default='jev-latest')
    ap.add_argument('--qstyle', default='v2', choices=['v1', 'v2'],
                    help='问题措辞: v2=判据式(默认) / v1=旧确认式(做A/B对照)')
    ap.add_argument('--excls-scope', default='window', choices=['window', 'full', 'off'],
                    help='B模式排除词判定: window=命中点±near(默认) / full=全文(旧行为) / off=不看')
    a = ap.parse_args()
    modes = [m.strip().upper() for m in a.modes.split(',') if m.strip()]

    lex = json.load(open(LEX, encoding='utf-8'))
    labels = merge_label_rows(lex['labels'])  # 同三元组只问一次; 组内词表/行号保留

    # 样本清单(=抽样阶段产物): 样本号 -> (csv_row_no, 入样原因)
    # 金标准(答卷)文件绝不进打标管道
    want = {}
    reason = {}
    only = set(x.strip() for x in a.samples.split(',') if x.strip())
    for r in csv.DictReader(open(a.gold, encoding='utf-8-sig')):
        if not only or r['样本号'] in only:
            want[int(r['CSV记录序号'])] = r['样本号']
            reason[r['样本号']] = (r.get('入样原因') or '').strip()
    if not want:
        print('!! 无匹配样本'); return 1

    os.makedirs(OUTD, exist_ok=True)
    rows = {}
    with open(V3, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            n = int(row['csv_row_no'])
            if n in want:
                rows[want[n]] = row
    print('样本=%d  模式=%s  措辞=%s  预算=%d tok(含state)  截断=%d 字' % (
        len(rows), ','.join(modes), a.qstyle, a.budget, a.max_chars))
    print('%-6s %-4s %6s %6s %9s %s' % ('样本', '模式', '问题数', '块数', '估tok/帖', '备注'))

    for s in sorted(rows):
        row = rows[s]
        text_full = row['full_text'] or ''
        truncated = len(text_full) > a.max_chars
        text = text_full[:a.max_chars]
        state = {'平台': row['site_name'], '内容': text}
        with open(os.path.join(OUTD, s + '.text.txt'), 'w', encoding='utf-8') as f:
            f.write(text)
        json.dump({'sample': s, 'csv_row_no': int(row['csv_row_no']), 'id': row['id'],
                   'site_name': row['site_name'], 'url': row['url'], 'publish_time': row['publish_time'],
                   'char_count_raw': int(row['char_count_raw']), 'char_count_full': int(row['char_count_full']),
                   'truncated': truncated, 'max_chars': a.max_chars, 'reason': reason.get(s, '')},
                  open(os.path.join(OUTD, s + '.meta.json'), 'w', encoding='utf-8'), ensure_ascii=False)

        with open(os.path.join(OUTD, s + '.jsonl'), 'w', encoding='utf-8') as f:
            for mode in modes:
                if mode == 'B':
                    # 粗筛用"送进模型的那段文本", 与 schemes.py 保持一致
                    pool = [L for L in labels if screen_label(text, L, a.excls_scope)]
                else:
                    pool = labels
                qs = {'q%04d' % L['idx']: {'type': 'noul', 'instructions': q_instruction(L, mode, a.qstyle)}
                      for L in pool}
                chunks = build_chunks(state, qs, a.budget)
                tot = 0
                for i, ch in enumerate(chunks, 1):
                    payload = {'sample': s, 'mode': mode, 'chunk': i, 'nchunks': len(chunks),
                               'model': a.model, 'state': state, 'questions': ch,
                               'meta': {'est_tokens': est_tokens(json.dumps({'state': state, 'questions': ch}, ensure_ascii=False)),
                                        'n_q': len(ch), 'truncated': truncated}}
                    f.write(json.dumps(payload, ensure_ascii=False) + '\n')
                    tot += payload['meta']['est_tokens']
                print('%-6s %-4s %6d %6d %9d %s' % (s, mode, len(qs), len(chunks), tot,
                                                     '截断' if truncated else ''))
    print('\n完成: 请求体在 %s' % OUTD)
    print('注意: 本入口产物无 scheme 字段, jev_client 的 --scheme 过滤不到;'
          ' 日常请用 core\\schemes.py')


if __name__ == '__main__':
    sys.exit(main() or 0)
