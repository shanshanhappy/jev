# -*- coding: utf-8 -*-
r"""jev_client.py  Jev API 客户端
读取 out/payloads/*.jsonl, 逐条POST到Jev API, 结果写入 out/raw/*.jsonl
支持 --scheme 参数过滤指定方案的请求
"""
import argparse, glob, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD_DIR = os.path.join(ROOT, 'out', 'payloads')
RAW_DIR = os.path.join(ROOT, 'out', 'raw')
from flow_rules import apply_dynamic_filter


def post_once(base, key, body, timeout, long_context=False, endpoint='/v1/systemone'):
    # endpoint 必须可配: 官方是 /v1/systemone, OpenRouter 也有 /v1/systemone(同一协议),
    # 另提供 /alpha/decisions。此前这里写死 /v1/systemone, platforms.json 的 endpoint_path 成了死配置。
    url = base.rstrip('/') + (endpoint or '/v1/systemone')
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Authorization', 'Bearer ' + key)
    req.add_header('Content-Type', 'application/json')
    # OpenRouter 排行榜用的可选头, 官方会忽略, 不影响结果
    req.add_header('HTTP-Referer', 'https://github.com/local/jev-batch')
    req.add_header('X-Title', 'jev-batch')
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


def _answers_of(resp):
    """取出答案字典。官方返回 answers; OpenRouter 的 decisions 端点若换了外层键名也能兜住。"""
    if not isinstance(resp, dict):
        return {}
    for k in ('answers', 'decisions', 'results'):
        v = resp.get(k)
        if isinstance(v, dict):
            return v
    return {}


def _usage_of(resp):
    """统一 usage 口径: 官方给 input_tokens/output_tokens, OpenAI 风格给 prompt_tokens/completion_tokens。"""
    u = (resp or {}).get('usage') if isinstance(resp, dict) else None
    if not isinstance(u, dict):
        return {'input_tokens': 0, 'output_tokens': 0}
    return {
        'input_tokens': _int(u.get('input_tokens', u.get('prompt_tokens'))),
        'output_tokens': _int(u.get('output_tokens', u.get('completion_tokens'))),
    }


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
    g = ap.add_mutually_exclusive_group(required=False)
    g.add_argument('--payload', help='单个 jsonl 文件')
    g.add_argument('--dir', help='payload 目录(配合 --samples)')
    ap.add_argument('--samples', default='', help='逗号分隔; --dir 模式必填')
    ap.add_argument('--all', action='store_true', help='--dir 模式下处理目录内全部 jsonl')
    ap.add_argument('--scheme', default='', help='过滤指定方案(如 B/H/RAG/Hybrid/Speculative); 空=不过滤')
    ap.add_argument('--platform', default='', help='从 core/platforms.json 加载平台配置(如 official/edgeone/openrouter); 空=用该文件的 default 字段')
    ap.add_argument('--base', default=os.environ.get('JEV_BASE_URL', 'https://api.typesafe.ai'))
    ap.add_argument('--key', default=os.environ.get('JEV_API_KEY', ''))
    ap.add_argument('--model', default='jev-latest')
    ap.add_argument('--endpoint', default='',
                    help='接口路径; 空=用平台配置的 endpoint_path(默认 /v1/systemone)。'
                         'OpenRouter 也可用 /alpha/decisions')
    ap.add_argument('--selftest', action='store_true',
                    help='只发 1 个最小请求并把完整响应打出来, 用来验证 key/端点/解析, 花费约 0.0001 美元')
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

    # --selftest 不需要 payload, 其余模式必须给 --payload 或 --dir
    if not a.selftest and not (a.payload or a.dir):
        ap.error('one of the arguments --payload --dir is required')

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
        if not a.endpoint:
            a.endpoint = p.get('endpoint_path', '/v1/systemone')
        print('使用平台配置: %s (%s)' % (a.platform, p['name']))
        print('  端点: %s%s   模型: %s' % (a.base.rstrip('/'), a.endpoint, a.model))
    if not a.endpoint:
        a.endpoint = '/v1/systemone'
    if not a.dry_run and not a.key:
        print('!! 缺少 API key: 设 $env:JEV_API_KEY 或 --key (或用 --dry-run)')
        return 1

    if a.selftest:
        # 最小成本验证: 1 帖 1 问, 约 60 tok, 花费可忽略。响应原样打印, 便于确认解析字段。
        body = {'model': a.model,
                'state': '这款双抗精华我用了两个月, 皮肤明显透亮, 已经回购第三次了。',
                'questions': {'q0001': {'type': 'noul',
                                        'instructions': '原文是否明确表达「回购」？只有原文有对应表述才算。'}}}
        print('\n[selftest] POST %s%s' % (a.base.rstrip('/'), a.endpoint))
        print('[selftest] model=%s 问题=1' % a.model)
        http, resp, ms = post_once(a.base, a.key, body, a.timeout, a.long_context, a.endpoint)
        print('[selftest] http=%s  %sms' % (http, ms))
        print('[selftest] 响应:\n%s' % json.dumps(resp, ensure_ascii=False, indent=2)[:2000])
        ans = _answers_of(resp)
        if http == 200 and ans:
            print('[selftest] ✅ 解析到 %d 个答案: %s' % (len(ans), list(ans.keys())[:5]))
            print('[selftest] usage: %s' % _usage_of(resp))
            return 0
        print('[selftest] ❌ 没解析到 answers, 检查上面的响应结构')
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
            # 产物命名是 {样本}__{方案}__{模式}.jsonl, 一个样本可能有多个方案/多个块。
            # 旧版按 {样本}.jsonl 拼路径(那是已删除的 jev_prompts.py 的命名), 对现有产物
            # 必然 FileNotFoundError, 故改为按前缀匹配, 并保留旧命名兜底。
            files, missing = [], []
            for x in ss:
                cand = sorted(glob.glob(os.path.join(a.dir, x + '__*.jsonl')))
                if not cand:
                    old = os.path.join(a.dir, x + '.jsonl')
                    cand = [old] if os.path.exists(old) else []
                if not cand:
                    missing.append(x)
                files.extend(cand)
            if missing:
                print('!! 目录 %s 下找不到这些样本的请求体: %s' % (a.dir, ', '.join(missing)))
            if not files:
                return 1

    # 过滤方案
    if a.scheme:
        scheme_lower = a.scheme.lower()
        filtered = []
        for fp in files:
            with open(fp, encoding='utf-8') as _f:
                for ln in _f:
                    p = json.loads(ln)
                    if p.get('scheme', '').lower() == scheme_lower:
                        filtered.append(fp)
                        break
        files = filtered
        print('过滤方案=%s: %d个文件' % (a.scheme, len(files)))
        if not files:
            print('!! 没有文件匹配方案 %s —— 检查 --scheme 拼写, 或 --dir 是否指向了别的方案目录' % a.scheme)
            return 1

    outdir = os.path.abspath(a.raw_dir) if a.raw_dir else os.path.join(ROOT, 'out', 'raw')
    os.makedirs(outdir, exist_ok=True)
    # 每次客户端执行一个批次号；多阶段方案只按本批第一阶段响应筛选，
    # 下游也只取每样本/阶段最新批次，防止旧候选在新结果里残留。
    run_id = datetime.now().strftime('%Y%m%dT%H%M%S') + '_%d' % os.getpid()
    stage_answers = {}
    print('原始响应目录: %s' % outdir)
    total_ok = total_err = 0
    recs = []       # 本批记录，供 print_summary 统计
    dry_n = dry_q = dry_est = 0

    for fp in files:
        if not os.path.exists(fp):
            print('跳过(不存在):', fp); continue
        with open(fp, encoding='utf-8') as _f:
          for ln in _f:
            p = json.loads(ln)
            # 方案过滤(行级)
            if a.scheme and p.get('scheme', '').lower() != scheme_lower:
                continue
            
            est = p.get('meta', {}).get('est_tokens', 0)
            if a.dry_run:
                print('[dry] %s scheme=%s stage=%s chunk=%s/%s 问题=%d 估tok=%d -> POST %s%s'
                      % (p['sample'], p.get('scheme', '-'), p.get('stage', '-'), p['chunk'], p['nchunks'], len(p['questions']), est, a.base, a.endpoint))
                dry_n += 1
                dry_q += len(p['questions'])
                dry_est += est
                continue

            # H 的 leaf / Speculative 的 pass2 必须在这里按本批第一阶段概率裁剪。
            # dry-run 没有真实概率，只展示候选上限，不假装已经完成动态筛选。
            sample_flow = stage_answers.setdefault(p['sample'], {})
            questions, flow_info = apply_dynamic_filter(p, sample_flow)
            # 输出路径 == 输入请求体路径时, 'a' 会把响应追加进请求体文件,
            # 循环继续读该文件时会把响应行当请求行解析 -> KeyError: 'state'。
            # 必须在发请求之前拦，否则白花一次调用。
            of = os.path.join(outdir, '%s__%s__%s.jsonl' % (p['sample'], p.get('scheme', ''), p['mode']))
            if os.path.abspath(of) == os.path.abspath(fp):
                print('!! 输出与输入是同一个文件(%s)，请换 --raw-dir' % of)
                return 1
            if not questions:
                rec = {'sample': p['sample'], 'mode': p['mode'], 'stage': p.get('stage', 'leaf'),
                       'scheme': p.get('scheme', ''), 'chunk': p['chunk'], 'nchunks': p['nchunks'],
                       'qstyle': p.get('meta', {}).get('qstyle', ''), 'run_id': run_id,
                       'ts': datetime.now().isoformat(timespec='seconds'), 'http': 200, 'skipped': True,
                       'latency_ms': 0, 'est_tokens': 0, 'base': a.base, 'sent_model': a.model,
                       'flow': flow_info,
                       'body': {'model': a.model, 'answers': {}, 'usage': {'input_tokens': 0, 'output_tokens': 0}}}
                with open(of, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + '\n')
                print('SKIP %s scheme=%s stage=%s c%s/%s: 动态筛选后0问 (%s)'
                      % (p['sample'], p.get('scheme', '-'), p.get('stage', '-'), p['chunk'], p['nchunks'], flow_info))
                continue
            body = {'model': a.model, 'state': p['state'], 'questions': questions}

            http, resp, ms = None, None, None
            for attempt in range(1, a.retry + 1):
                http, resp, ms = post_once(a.base, a.key, body, a.timeout, a.long_context, a.endpoint)
                if http == 200 or http not in (0, 429, 500, 502, 503, 504):
                    break
                wait = 2 ** attempt
                print('  %d 重试%d/%d (等%ds): %s' % (http, attempt, a.retry, wait, str(resp)[:120]))
                time.sleep(wait)

            # 统一响应口径后再落盘: 评测端只认 body.answers / body.usage.input_tokens,
            # 不同网关(prompt_tokens / decisions 外层)会静默算成 0 分, 这里一次补齐。
            if http == 200 and isinstance(resp, dict):
                if not isinstance(resp.get('answers'), dict):
                    _a = _answers_of(resp)
                    if _a:
                        resp['answers'] = _a
                _u = _usage_of(resp)
                if not isinstance(resp.get('usage'), dict):
                    resp['usage'] = _u
                else:
                    resp['usage'].setdefault('input_tokens', _u['input_tokens'])
                    resp['usage'].setdefault('output_tokens', _u['output_tokens'])
            
            rec = {'sample': p['sample'], 'mode': p['mode'], 'stage': p.get('stage', 'leaf'), 'scheme': p.get('scheme', ''),
                   'chunk': p['chunk'], 'nchunks': p['nchunks'], 'run_id': run_id, 'flow': flow_info,
                   # 措辞版本必须跟着响应落盘：payload 里记了 qstyle, 但 raw 不记的话
                   # 评测端无从判断这批概率是 v2 还是 v3 产出的, 混批会静默算错指标
                   'qstyle': p.get('meta', {}).get('qstyle', ''),
                   'ts': datetime.now().isoformat(timespec='seconds'), 'http': http, 'latency_ms': ms,
                   'est_tokens': est, 'base': a.base, 'sent_model': a.model, 'body': resp}
            with open(of, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            usage = _usage_of(resp)
            ans = _answers_of(resp)
            ok = http == 200
            if ok:
                sample_flow.setdefault(p.get('stage', 'leaf'), {}).update(ans)
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
                              '平台': a.platform or 'default', 'run_id': run_id,
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
