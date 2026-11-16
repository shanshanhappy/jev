# -*- coding: utf-8 -*-
r"""jev_client.py  Jev API 客户端
读取 out/payloads/*.jsonl, 逐条POST到Jev API, 结果写入 out/raw/*.jsonl
支持 --scheme 参数过滤指定方案的请求
"""
import argparse, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD_DIR = os.path.join(ROOT, 'out', 'payloads')
RAW_DIR = os.path.join(ROOT, 'out', 'raw')


def post_once(base, key, body, timeout, long_context=False):
    url = base.rstrip('/') + '/v1/systemone'
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Authorization', 'Bearer ' + key)
    req.add_header('Content-Type', 'application/json')
    if long_context:
        req.add_header('TypeSafe-Danger-Zone-Long-Context', 'true')
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200, json.loads(r.read().decode('utf-8')), round((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode('utf-8'))
        except Exception:
            err = {'raw': '<非JSON错误体>'}
        return e.code, err, round((time.time() - t0) * 1000)
    except urllib.error.URLError as e:
        # DNS 失败 / 连接被拒 / TLS 错误等，按可重试验待
        return 0, {'error': 'URLError: %s' % (getattr(e, 'reason', e),)}, round((time.time() - t0) * 1000)
    except (TimeoutError, OSError) as e:
        # 读超时、连接被重置等
        return 0, {'error': '%s: %s' % (type(e).__name__, e)}, round((time.time() - t0) * 1000)
    except Exception as e:
        # 兜底：任何异常都不允许中断整批
        return 0, {'error': '%s: %s' % (type(e).__name__, e)}, round((time.time() - t0) * 1000)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _count_hit(raw_ans, threshold):
    """单个 answer 是否算命中：noul/score 取数值 >= 阈值；choice 取有值即算"""
    if not isinstance(raw_ans, dict):
        return 0
    t = raw_ans.get('type', '')
    if t == 'choice':
        return 1 if raw_ans.get('choice') else 0
    v = raw_ans.get('noul', raw_ans.get('score'))
    try:
        return 1 if float(v) >= threshold else 0
    except (TypeError, ValueError):
        return 0


def print_summary(recs, threshold, price_per_m=0.042):
    """打印本批运行报告（只统计本次运行，不含 out/raw 里的历史记录）"""
    n = len(recs)
    ok = [r for r in recs if r['http'] == 200]
    bad = [r for r in recs if r['http'] != 200]
    in_tok = sum(r['in_tok'] for r in ok)
    out_tok = sum(r['out_tok'] for r in ok)
    est = sum(r['est'] for r in recs)
    ms = [r['ms'] for r in ok] or [0]
    answers = sum(r['n_ans'] for r in ok)
    hits = sum(r['hits'] for r in ok)
    per = [r['hits'] for r in ok] or [0]
    cost = in_tok / 1e6 * price_per_m

    print('\n' + '=' * 52)
    print('本批运行报告')
    print('-' * 52)
    print('请求数        %d   成功 %d   失败 %d' % (n, len(ok), len(bad)))
    print('命中阈值      %.2f' % threshold)
    print('input_tokens  合计 %s   均 %s/条'
          % (format(in_tok, ','), format(round(in_tok / len(ok)) if ok else 0, ',')))
    print('output_tokens 合计 %s' % format(out_tok, ','))
    print('耗时          均 %.1fs   最长 %.1fs   总计 %.1f 分钟'
          % (sum(ms) / len(ms) / 1000, max(ms) / 1000, sum(ms) / 1000 / 60))
    if est:
        print('估算 vs 实测  估 %s  实 %s  偏差 %+.0f%%'
              % (format(est, ','), format(in_tok, ','), (in_tok - est) / est * 100))
    if answers:
        print('标签命中      %s / %s 问 (%.1f%%)'
              % (format(hits, ','), format(answers, ','), 100.0 * hits / answers))
        print('每帖命中      最少 %d   最多 %d   均 %.1f'
              % (min(per), max(per), sum(per) / len(per)))
    print('折算成本      $%.4f (≈¥%.2f)  按 $%s/M input'
          % (cost, cost * 7.2, price_per_m))
    low = sorted([(r['sample'], r['hits']) for r in ok if r['hits'] < 5])
    if low:
        print('低命中(<5)    %s' % ' '.join('%s(%d)' % x for x in low))
    if bad:
        print('-' * 52)
        print('失败明细:')
        for r in bad[:20]:
            print('  %-6s http=%-4s %s' % (r['sample'], r['http'], r['err'][:90]))
        if len(bad) > 20:
            print('  ... 共 %d 条失败，完整响应见 out/raw/' % len(bad))
    print('=' * 52)


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--payload', help='单个 jsonl 文件')
    g.add_argument('--dir', help='payload 目录(配合 --samples)')
    ap.add_argument('--samples', default='', help='逗号分隔; --dir 模式必填')
    ap.add_argument('--all', action='store_true', help='--dir 模式下处理目录内全部 jsonl')
    ap.add_argument('--scheme', default='', help='过滤指定方案(如 B/H/RAG/Hybrid/Speculative); 空=不过滤')
    ap.add_argument('--platform', default='', help='从 core/platforms.json 加载平台配置(如 official/edgeone/openrouter); 空=用该文件的 default 字段')
    ap.add_argument('--base', default=os.environ.get('JEV_BASE_URL', 'https://api.typesafe.ai'))
    ap.add_argument('--key', default=os.environ.get('JEV_API_KEY', ''))
    ap.add_argument('--model', default='jev-latest')
    ap.add_argument('--sleep', type=float, default=0.2)
    ap.add_argument('--retry', type=int, default=3)
    ap.add_argument('--timeout', type=int, default=120)
    ap.add_argument('--long-context', action='store_true', help='加 262k Danger-Zone 头(官方支持)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-csv', action='store_true', help='跑完不自动导出 CSV(默认会自动导出 打标明细/打标概览)')
    ap.add_argument('--threshold', type=float, default=None,
                    help='导出的 noul 命中阈值；不填则用 tools/raw_to_csv.py 的 DEFAULT_THRESHOLD')
    ap.add_argument('--top-n', type=int, default=8, help='概览表每帖最多展示几个 TOPIC ANGLE，默认 8')
    ap.add_argument('--raw-dir', default='',
                    help='原始响应输出目录; 空=out\\raw。多方案并行时建议每方案一个目录, '
                         '如 --raw-dir out\\raw_A1 (否则导出的 CSV 会把多个方案混在一起)')
    ap.add_argument('--proxy', default='',
                    help='显式 HTTP/HTTPS 代理, 如 http://127.0.0.1:7897。'
                         '官方 api.typesafe.ai 对中国大陆返回 451, 需走代理; '
                         '留空则沿用系统环境变量')
    a = ap.parse_args()

    if a.proxy:
        for k in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'):
            os.environ[k] = a.proxy
        print('使用代理: %s' % a.proxy)

    # 命中阈值只认一个来源：tools/raw_to_csv.py 的 DEFAULT_THRESHOLD，避免两个入口默认值打架
    try:
        _tools = os.path.join(ROOT, 'tools')
        if _tools not in sys.path:
            # append 而非 insert(0)：避免 tools\ 下的同名模块盖掉 core\ 的
            sys.path.append(_tools)
        import raw_to_csv as _r2c
        a.threshold = a.threshold if a.threshold is not None else _r2c.DEFAULT_THRESHOLD
        _TH_SRC = 'tools/raw_to_csv.py 默认值'
    except Exception:
        _r2c = None
        a.threshold = a.threshold if a.threshold is not None else 0.6
        _TH_SRC = '内置兜底值'


    # 加载平台配置：未指定 --platform 时，自动使用 platforms.json 的 default 字段
    platforms_file = os.path.join(ROOT, 'core', 'platforms.json')
    if not a.platform and os.path.exists(platforms_file):
        try:
            with open(platforms_file, encoding='utf-8-sig') as f:
                a.platform = json.load(f).get('default', '')
        except Exception:
            a.platform = ''
    if a.platform:
        if not os.path.exists(platforms_file):
            print('!! 配置文件不存在: %s' % platforms_file)
            return 1
        with open(platforms_file, encoding='utf-8-sig') as f:
            platforms = json.load(f)
        if a.platform not in platforms['platforms']:
            print('!! 未找到平台: %s (可选: %s)' % (a.platform, ', '.join(platforms['platforms'].keys())))
            return 1
        p = platforms['platforms'][a.platform]
        # 命令行参数优先，否则用配置文件
        if not a.base or a.base == 'https://api.typesafe.ai':
            a.base = p['base_url']
        if not a.key:
            a.key = p['api_key']
        if a.model == 'jev-latest':
            a.model = p.get('model', 'jev-latest')
        print('使用平台配置: %s (%s)' % (a.platform, p['name']))
    if not a.dry_run and not a.key:
        print('!! 缺少 API key: 设 $env:JEV_API_KEY 或 --key (或用 --dry-run)')
        return 1

    files = []
    if a.payload:
        files = [a.payload]
    else:
        if a.all:
            files = [os.path.join(a.dir, fn) for fn in sorted(os.listdir(a.dir)) if fn.endswith('.jsonl')]
        else:
            ss = [x.strip() for x in a.samples.split(',') if x.strip()]
            if not ss:
                print('!! --dir 模式请用 --samples S001,S002 或 --all')
                return 1
            files = [os.path.join(a.dir, x + '.jsonl') for x in ss]

    # 过滤方案
    if a.scheme:
        scheme_lower = a.scheme.lower()
        filtered = []
        for fp in files:
            for ln in open(fp, encoding='utf-8'):
                p = json.loads(ln)
                if p.get('scheme', '').lower() == scheme_lower:
                    filtered.append(fp)
                    break
        files = filtered
        print('过滤方案=%s: %d个文件' % (a.scheme, len(files)))

    outdir = os.path.abspath(a.raw_dir) if a.raw_dir else os.path.join(ROOT, 'out', 'raw')
    os.makedirs(outdir, exist_ok=True)
    print('原始响应目录: %s' % outdir)
    total_ok = total_err = 0
    recs = []       # 本批记录，供 print_summary 统计
    dry_n = dry_q = dry_est = 0

    for fp in files:
        if not os.path.exists(fp):
            print('跳过(不存在):', fp); continue
        for ln in open(fp, encoding='utf-8'):
            p = json.loads(ln)
            # 方案过滤(行级)
            if a.scheme and p.get('scheme', '').lower() != scheme_lower:
                continue
            
            body = {'model': a.model, 'state': p['state'], 'questions': p['questions']}
            est = p.get('meta', {}).get('est_tokens', 0)
            if a.dry_run:
                print('[dry] %s scheme=%s stage=%s chunk=%s/%s 问题=%d 估tok=%d -> POST %s/v1/systemone'
                      % (p['sample'], p.get('scheme', '-'), p.get('stage', '-'), p['chunk'], p['nchunks'], len(p['questions']), est, a.base))
                dry_n += 1
                dry_q += len(p['questions'])
                dry_est += est
                continue
            http, resp, ms = None, None, None
            for attempt in range(1, a.retry + 1):
                http, resp, ms = post_once(a.base, a.key, body, a.timeout, a.long_context)
                if http == 200 or http not in (0, 429, 500, 502, 503, 504):
                    break
                wait = 2 ** attempt
                print('  %d 重试%d/%d (等%ds): %s' % (http, attempt, a.retry, wait, str(resp)[:120]))
                time.sleep(wait)
            
            rec = {'sample': p['sample'], 'mode': p['mode'], 'stage': p.get('stage', 'leaf'), 'scheme': p.get('scheme', ''),
                   'chunk': p['chunk'], 'nchunks': p['nchunks'],
                   'ts': datetime.now().isoformat(timespec='seconds'), 'http': http, 'latency_ms': ms,
                   'est_tokens': est, 'base': a.base, 'sent_model': a.model, 'body': resp}
            of = os.path.join(outdir, '%s__%s__%s.jsonl' % (p['sample'], p.get('scheme', ''), p['mode']))
            with open(of, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            usage = resp.get('usage', {}) if isinstance(resp, dict) else {}
            ans = resp.get('answers', {}) if isinstance(resp, dict) else {}
            ok = http == 200
            total_ok += 1 if ok else 0
            total_err += 0 if ok else 1
            recs.append({
                'sample': p['sample'], 'http': http, 'ms': ms or 0, 'est': est,
                'in_tok': _int(usage.get('input_tokens')),
                'out_tok': _int(usage.get('output_tokens')),
                'n_ans': len(ans),
                'hits': sum(_count_hit(v, a.threshold) for v in ans.values()) if ok else 0,
                'err': str(resp)[:160],
            })
            print('%s %s scheme=%s stage=%s c%s/%s http=%s %sms in_tok=%s %s'
                  % ('OK ' if ok else 'ERR', p['sample'], p.get('scheme', '-'), p.get('stage', '-'), p['chunk'], p['nchunks'], http, ms,
                     usage.get('input_tokens', '-'), '' if ok else str(resp)[:160]))
            time.sleep(a.sleep)

    if a.dry_run:
        print('\n[dry-run] 未发送请求: %d 条, 问题 %d 个, 估算 input_tokens 合计 %s'
              % (dry_n, dry_q, format(dry_est, ',')))
        return 0

    if recs:
        print_summary(recs, a.threshold)

    print('\n完成: 成功%d 失败%d ; 原始响应目录 %s' % (total_ok, total_err, outdir))
    try:
        import genlog
        genlog.record('core/jev_client.py', '调用 Jev 取响应', outdir, 'dir', len(recs),
                      note='追加写入：重跑不会覆盖，旧响应会一直留着',
                      params={'成功': total_ok, '失败': total_err,
                              '样本数': len({r['sample'] for r in recs}),
                              'input_tokens合计': sum(r['in_tok'] for r in recs),
                              'output_tokens合计': sum(r['out_tok'] for r in recs),
                              '平台': a.platform or 'default',
                              'model': a.model or '-',
                              '阈值': a.threshold if a.threshold is not None else 'DEFAULT'})
    except Exception as _e:
        print('!! 生成日志写入失败(不影响结果): %s' % _e)
    if not a.no_csv and (total_ok or total_err) and _r2c is not None:
        try:
            print()
            pdir = a.dir if a.dir else os.path.dirname(a.payload or '')
            # 自定义 --raw-dir 时，CSV 必须落到同目录；否则会覆盖 out\ 下其它方案的交付文件
            _def_raw = os.path.join(ROOT, 'out', 'raw')
            csv_out = (os.path.join(ROOT, 'out')
                       if os.path.abspath(outdir) == os.path.abspath(_def_raw) else outdir)
            p1, p2, n1, n2 = _r2c.export(threshold=a.threshold, top_n=a.top_n,
                                         raw_dir=outdir, payload_dir=pdir or None, outdir=csv_out)
            if n1 or n2:
                print('（本次只跑了 %d 条，但导出是 %s 全量合并：概览/明细都是 %d 条样本；CSV 在 %s）'
                      % (total_ok + total_err, os.path.basename(outdir), n2, csv_out))
        except Exception as e:
            print('!! CSV 自动导出失败（原始 jsonl 已保存，可手动执行 python tools/raw_to_csv.py）: %s' % e)


if __name__ == '__main__':
    sys.exit(main() or 0)
