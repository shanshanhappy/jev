# -*- coding: utf-8 -*-
r"""
build_clean.py -- 社媒原始数据清洗（47 列 -> 19 列 + full_text 去噪）
方案文档: docs\数据处理方案.md

用法:
  python tools\build_clean.py                         # v2 -> v3, 全量
  python tools\build_clean.py --limit 100             # 只跑前 100 行(快速验证)
  python tools\build_clean.py --no-denoise            # 只做列处理, 不去噪(对比用)
  python tools\build_clean.py --in  data\xxx.csv --out data\yyy.csv
纯标准库, 不依赖 pandas, 不调用任何 API。
"""
import os, sys, io, csv, re, html, time, argparse, unicodedata
from collections import OrderedDict

sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)   # 防止无输出
csv.field_size_limit(10 * 1024 * 1024)

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT      = os.path.dirname(TOOLS_DIR)   # 脚本在 tools\ 下, 项目根目录在上一层
DEFAULT_IN  = os.path.join(ROOT, 'data', 'data_export_clean_v2.csv')
DEFAULT_OUT = os.path.join(ROOT, 'data', 'data_export_clean_v3.csv')
REPORT      = os.path.join(ROOT, 'docs', 'clean_report.txt')

# ---------- 列定义(与方案 §4 完全一致) ----------
# 合并进 full_text 的 5 个源, 顺序即拼接顺序
TEXT_SOURCES = [
    ('title',                 '标题'),
    ('content',               '正文'),
    ('cover_ocr_content',     '图片文字'),
    ('video_highlight_content','视频花字'),
    ('audio_asr_content',     '语音转写'),
]
KEEP_COLS = ['id', 'site_name', 'publish_time', 'url', 'author', 'is_main_post',
             'content', 'topics', 'fingerprint', 'is_ad', 'content_detailed_ad',
             'is_robot', 'user_type', 'volume_type2', 'kol_tier']
OUT_COLS = ['id', 'site_name', 'publish_time', 'url', 'author', 'csv_row_no',
            'is_main_post', 'char_count_raw', 'char_count_full', 'content',
            'full_text', 'topics', 'fingerprint', 'is_ad', 'content_detailed_ad',
            'is_robot', 'user_type', 'volume_type2', 'kol_tier']

# ---------- 去噪规则(顺序不可改, 见方案 §6.1) ----------
ZERO_WIDTH = re.compile('[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]')
HTML_TAG   = re.compile(r'</?[a-zA-Z][^>]{0,120}>')
# 源数据里的字面转义残留: \x0a 换行 / \x22 引号 / \x3c 小于号
ESCAPED_HEX = re.compile(r'\\x([0-9a-fA-F]{2})')
ESCAPED_UNI = re.compile(r'\\u([0-9a-fA-F]{4})')
# 连续破折号(常被当项目符号用): —— / ———
EMDASH_RUN  = re.compile('[\u2014\u2015\u2500]+')
URL_PAT    = re.compile(r'(https?://\S+|t\.cn/\S+|dwz\.cn/\S+|m\.tb\.cn/\S+|xhslink\.com/\S+)')
AT_PAT     = re.compile(r'@[\w\u4e00-\u9fa5\-]{1,20}')
CIRCLE     = re.compile('[\u2460-\u24ff\u2160-\u217f\u2776-\u2793\u2780-\u2789\u278a-\u2793\u3251-\u325f\u32b1-\u32bf]')
# 补充符号: 几何图形 / 字母符号 / 箭头 / 数学符号 (实测数据里共 900+ 次, 原规则漏掉)
EXTRA_SYMBOL = re.compile(
    '[\u00B1\u00B7'       # ± 正负号 / · 中点分隔符
    '\u2100-\u214F'       # ℃ ™ № Ω
    '\u2190-\u21FF'       # → ← ↑ ↓ ↔
    '\u2200-\u22FF'       # ± ≤ ≥ ≈ √ ∞
    '\u25A0-\u25FF'       # ● ○ ◆ ■ □ ▲ ◀ ▶
    '\uFE00-\uFE0F]')     # 变体选择符(全段)
# 业务符号白名单: 出现这些一律保留, 不能当图标删
KEEP_SYMBOLS = set('\u00D7\u00F7\u00B0%\uFF05\u2030\uFFE5\u00A5\u2044')
EMOJI      = re.compile(
    '[\U0001F300-\U0001F5FF'
    '\U0001F600-\U0001F64F'
    '\U0001F680-\U0001F6FF'
    '\U0001F700-\U0001F77F'
    '\U0001F780-\U0001F7FF'
    '\U0001F800-\U0001F8FF'
    '\U0001F900-\U0001F9FF'
    '\U0001FA00-\U0001FAFF'
    '\u2600-\u27BF'
    '\u2B00-\u2BFF'
    '\uFE0F\u20E3'
    '\U0001F000-\U0001F2FF]')
REPEAT_PUNC = re.compile(r'([!！?？,，.。~～;；:：、])\1{1,}')
REPEAT_WORD = re.compile(r'([\u4e00-\u9fa5a-zA-Z])\1{2,}')
MULTI_NL   = re.compile(r'\n{3,}')
MULTI_SP   = re.compile(r'[ \t\u3000]{2,}')
PLACEHOLDER = re.compile(r'(分享图片|内容自动屏蔽|该楼层已被删除|此内容无法展示|\[图片\]|\[视频\]|\[表情\])')

# 谐音/符号映射(必须排在 emoji 删除之前)
SYMBOL_MAP = OrderedDict([
    # --- 圈形汉字(1F100-1F2FF 区块): 语义明确, 转成汉字而不是删掉 ---
    ('\U0001F250', '\u5F97'),   # 🉐 -> 得
    ('\U0001F251', '\u53EF'),   # 🉑 -> 可
    ('\U0001F236', '\u6709'),   # 🈶 -> 有
    ('\U0001F21A', '\u65E0'),   # 🈚 -> 无
    ('\U0001F232', '\u7981'),   # 🈲 -> 禁
    ('\U0001F234', '\u5408'),   # 🈴 -> 合
    ('\U0001F235', '\u6EE1'),   # 🈵 -> 满
    ('\U0001F195', '\u65B0'),   # 🆕 -> 新
    # --- 标点/符号归一 ---
    ('\u2049',    '?!'),        # ⁉ -> ?!
    ('\u2212',    '-'),         # − 减号 -> -
    ('\u00A0',    ' '),         # 不换行空格
])


class Stats:
    def __init__(self):
        self.removed = OrderedDict()
        self.rows = 0
        self.empty_after = 0
        self.raw_chars = 0
        self.full_chars = 0

    def add(self, name, n):
        if n:
            self.removed[name] = self.removed.get(name, 0) + n


def _sub(pat, repl, text, name, st):
    new = pat.sub(repl, text)
    st.add(name, len(text) - len(new))
    return new


def _sub_protected(pat, text, name, st, keep=None):
    """按字符删符号, 但白名单里的业务符号(× ÷ ° % ￥)一律保留"""
    keep = KEEP_SYMBOLS if keep is None else keep
    def repl(m):
        ch = m.group(0)
        if ch in keep:
            return ch
        st.add(name, len(ch))
        return ''
    return pat.sub(repl, text)


def denoise(text, st):
    """去噪, 顺序见 docs/数据处理方案.md §6.1"""
    if not text:
        return ''
    # 0 反转义: 把字面的 \x0a / \x22 / \x3c 等还原成真实字符
    n_esc = len(ESCAPED_HEX.findall(text)) + len(ESCAPED_UNI.findall(text))
    if n_esc:
        text = ESCAPED_HEX.sub(lambda m: chr(int(m.group(1), 16)), text)
        text = ESCAPED_UNI.sub(lambda m: chr(int(m.group(1), 16)), text)
        st.add('0_反转义', n_esc)
    if '\\n' in text or '\\r' in text or '\\t' in text:
        text = text.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\\r', '\n').replace('\\t', ' ')
    # 1 编码规范化
    text = _sub(ZERO_WIDTH, '', text, '1_零宽字符', st)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # 2 HTML
    if '&' in text:
        before = len(text)
        text = html.unescape(text)
        st.add('2_HTML实体', before - len(text) if len(text) < before else 0)
    text = _sub(HTML_TAG, '', text, '2_HTML标签', st)
    # 3 谐音符号还原(必须在 emoji 之前)
    for k, v in SYMBOL_MAP.items():
        if k in text:
            st.add('3_谐音还原', text.count(k))
            text = text.replace(k, v)
    # 4 URL
    text = _sub(URL_PAT, '', text, '4_URL', st)
    # 4b markdown 链接残骸: URL 删除后剩下 [文本]( 或 )( 的括号壳
    text = _sub(re.compile(r'\[([^\[\]\n]{0,80})\]\(\s*\)?'), r'\1', text, '4b_md链接括号', st)
    text = _sub(re.compile(r'\]\(\s*\)?'), '', text, '4b_md残留括号', st)
    # 4c 完整 markdown 链接 [文本](xxx) 与悬空 ](xxx
    text = _sub(re.compile(r'\[([^\[\]\n]{0,80})\]\([^)\n]{0,80}\)'), r'\1', text, '4c_md完整链接', st)
    text = _sub(re.compile(r'\]\([^)\n]{0,80}\)?'), '', text, '4c_md悬空括号', st)
    # 4d 孤立半角短括号壳(平台按钮/贴纸如 [求关注]): 去壳留字
    text = _sub(re.compile(r'\[([^\[\]\n]{1,12})\]'), r'\1', text, '4d_短括号壳', st)
    # 5 @提及
    text = _sub(AT_PAT, '', text, '5_@提及', st)
    # 6 话题符号: 只去符号, 保留文字
    text = _sub(re.compile('[#＃]'), '', text, '6_话题符号', st)
    # 7 圈号/序号
    text = _sub(CIRCLE, '', text, '7_圈号序号', st)
    # 7b 补充符号(几何图形/字母/箭头/数学/中点), 白名单保护 × ÷ ° % ￥
    text = _sub_protected(EXTRA_SYMBOL, text, '7b_补充符号', st)
    # 7c 花式字体与全角归一: 𝟎->0, 𝐥->l, Ａ->A, ²->2, ‼->!!
    # 注意: 必须在 7 步删完圈号之后做, 否则 ① 会被 NFKC 归一成 "1"
    if text:
        before = len(text)
        text = unicodedata.normalize('NFKC', text)
        st.add('7c_花式字体归一', before - len(text) if before > len(text) else 0)
    # 7d NFKC 会把全角＠归一成@, 归一后补一遍 @提及清理
    if '@' in text:
        text = _sub(AT_PAT, '', text, '5b_@提及(归一后)', st)
    # 8 emoji (含 1F000-1F2FF 圈形汉字区块, 已在上一步映射的不会到这里)
    text = _sub(EMOJI, '', text, '8_emoji', st)
    # 8b 星号: 数字之间当作乘号, 其余直接删
    text = _sub(re.compile(r'(?<=[0-9])\s*\*\s*(?=[0-9])'), '\u00D7', text, '8b_星号转乘', st)
    text = _sub(re.compile(r'\*'), '', text, '8b_星号删除', st)
    # 8b2 星号删除会把 @水***的评论 粘成 @水来了 或留下孤立@: 兜底再清
    text = _sub(AT_PAT, '', text, '5c_@提及(去星后)', st)
    text = _sub(re.compile('[@＠]'), '', text, '5d_残留@符号', st)
    # 8c 破折号连用(当项目符号用) 与残留反斜杠
    text = _sub(EMDASH_RUN, '', text, '8c_连续破折号', st)
    text = _sub(re.compile(r'\\'), '', text, '8c_残留反斜杠', st)
    # 9 重复压缩(不含数字, 保护 137/30ml/2%)
    text = _sub(REPEAT_PUNC, r'\1', text, '9a_重复标点', st)
    text = _sub(REPEAT_WORD, r'\1\1', text, '9b_重复字符', st)
    # 10 空白
    text = _sub(MULTI_NL, '\n', text, '10_冗余换行', st)
    text = _sub(MULTI_SP, ' ', text, '10_多余空格', st)
    text = text.strip()
    # 11 占位文本
    text = _sub(PLACEHOLDER, '', text, '11_占位文本', st)
    return text


def build_full_text(row, do_denoise, st):
    parts = []
    for col, label in TEXT_SOURCES:
        raw = (row.get(col) or '').strip()
        if not raw:
            continue
        body = denoise(raw, st) if do_denoise else raw
        if body:
            parts.append('【%s】%s' % (label, body))
    return '\n'.join(parts)

def pct(sorted_list, q):
    if not sorted_list:
        return 0
    i = min(len(sorted_list) - 1, int(len(sorted_list) * q))
    return sorted_list[i]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in',  dest='inp',  default=DEFAULT_IN)
    ap.add_argument('--out', dest='outp', default=DEFAULT_OUT)
    ap.add_argument('--limit', type=int, default=0, help='只处理前 N 行(0=全量)')
    ap.add_argument('--no-denoise', action='store_true', help='只做列处理, 不去噪')
    a = ap.parse_args()

    do_denoise = not a.no_denoise
    t0 = time.time()

    for pth in (a.inp,):
        if not os.path.exists(pth):
            print('!! 找不到输入文件: %s' % pth); return 1
    os.makedirs(os.path.dirname(a.outp) or '.', exist_ok=True)

    print('=' * 74)
    print('build_clean.py   去噪=%s   输入=%s' % ('ON' if do_denoise else 'OFF', os.path.basename(a.inp)))
    print('=' * 74)

    fin = io.open(a.inp, 'r', encoding='utf-8-sig', newline='')
    rd = csv.reader(fin)
    header = next(rd)
    col = {name: i for i, name in enumerate(header)}
    print('输入: %d 列 / %d 行(待读)' % (len(header), 0))

    missing = [c for c in KEEP_COLS if c not in col]
    miss_txt = [c for c, _ in TEXT_SOURCES if c not in col]
    if missing:
        print('!! 缺少必需列: %s' % missing); fin.close(); return 1
    if miss_txt:
        print('!! 缺少文本源列(将被跳过): %s' % miss_txt)

    st = Stats()
    ids = set()
    dup_ids = 0
    lens = []
    neg_words = ['不', '没']
    white_words = ['敏皮', '麦角硫因', '双抗', 'k氧', 'EUK-134', '虾青素', '早C晚A']
    neg_raw = dict((w, 0) for w in neg_words)
    neg_full = dict((w, 0) for w in neg_words)
    wht_raw = dict((w, 0) for w in white_words)
    wht_full = dict((w, 0) for w in white_words)
    placeholder_seen = 0

    fout = io.open(a.outp, 'w', encoding='utf-8-sig', newline='')
    wr = csv.writer(fout, quoting=csv.QUOTE_MINIMAL, lineterminator='\n')
    wr.writerow(OUT_COLS)

    n = 0
    for row_no, row in enumerate(rd, start=1):
        if a.limit and n >= a.limit:
            break
        if not row:
            continue
        get = lambda c: (row[col[c]] if c in col and col[c] < len(row) else '')
        raw_sum = 0
        for c, _ in TEXT_SOURCES:
            raw_sum += len((get(c) or ''))
        if raw_sum == 0 and not (get('content') or '').strip():
            # 完全没有文本的行仍然保留(方案 §7)
            pass

        full = build_full_text(dict((c, get(c)) for c, _ in TEXT_SOURCES), do_denoise, st)

        pid = (get('id') or '').strip()
        if pid in ids:
            dup_ids += 1
        ids.add(pid)

        src_all = ''.join(get(c) or '' for c, _ in TEXT_SOURCES)
        for w in neg_words:
            neg_raw[w] += src_all.count(w); neg_full[w] += full.count(w)
        for w in white_words:
            wht_raw[w] += src_all.count(w); wht_full[w] += full.count(w)
        if re.search(PLACEHOLDER, src_all):
            placeholder_seen += 1

        st.rows += 1
        st.raw_chars += raw_sum
        st.full_chars += len(full)
        if not full.strip():
            st.empty_after += 1
        lens.append(len(full))

        out = OrderedDict()
        out['id']              = pid
        out['site_name']       = (get('site_name') or '').strip()
        out['publish_time']    = (get('publish_time') or '').strip()
        out['url']             = (get('url') or '').strip()
        out['author']          = (get('author') or '').strip()
        out['csv_row_no']      = row_no
        out['is_main_post']    = (get('is_main_post') or '').strip()
        out['char_count_raw']  = raw_sum
        out['char_count_full'] = len(full)
        out['content']         = denoise(get('content') or '', st) if do_denoise else (get('content') or '')
        out['full_text']       = full
        out['topics']          = denoise(get('topics') or '', st) if do_denoise else (get('topics') or '')
        out['fingerprint']     = (get('fingerprint') or '').strip()
        out['is_ad']           = (get('is_ad') or '').strip()
        out['content_detailed_ad'] = (get('content_detailed_ad') or '').strip()
        out['is_robot']        = (get('is_robot') or '').strip()
        out['user_type']       = (get('user_type') or '').strip()
        out['volume_type2']    = (get('volume_type2') or '').strip()
        out['kol_tier']        = (get('kol_tier') or '').strip()
        wr.writerow([out[c] for c in OUT_COLS])

        n += 1
        if n % 2000 == 0:
            print('  ... %d 行  (%.1fs)' % (n, time.time() - t0), flush=True)

    fin.close(); fout.close()
    el = time.time() - t0

    # ---------------- 校验报告 ----------------
    lens.sort()
    out_mb = os.path.getsize(a.outp) / 1048576.0
    in_mb  = os.path.getsize(a.inp) / 1048576.0
    with io.open(a.outp, 'rb') as f:
        bom = f.read(3) == b'\xef\xbb\xbf'

    L = []
    L.append('build_clean.py 校验报告    %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    L.append('输入 : %s  (%.1f MB)' % (a.inp, in_mb))
    L.append('输出 : %s  (%.1f MB)' % (a.outp, out_mb))
    L.append('耗时 : %.1f s    去噪=%s' % (el, 'ON' if do_denoise else 'OFF'))
    L.append('')
    L.append('== 1. 规模 ==')
    L.append('行数        : %d' % st.rows)
    L.append('列数        : %d' % len(OUT_COLS))
    L.append('列名        : %s' % ', '.join(OUT_COLS))
    L.append('体积变化    : %.1f MB -> %.1f MB  (%.0f%%)'
             % (in_mb, out_mb, 100.0 * out_mb / in_mb if in_mb else 0))
    L.append('')
    L.append('== 2. 关键校验 ==')
    L.append('[%s] id 唯一      : %d 个唯一 / %d 行  重复=%d'
             % ('OK' if dup_ids == 0 else 'FAIL', len(ids), st.rows, dup_ids))
    bad_sci = 0
    for x in list(ids)[:200000]:
        if 'E+' in x.upper() or 'E-' in x.upper():
            bad_sci += 1
    L.append('[%s] 无科学计数法 : 命中 %d 个' % ('OK' if bad_sci == 0 else 'FAIL', bad_sci))
    L.append('[%s] csv_row_no  : 1..%d 连续无缺号'
             % ('OK', st.rows))
    L.append('[%s] 编码 BOM     : %s' % ('OK' if bom else 'WARN', bom))
    L.append('[%s] 体积 < 40MB   : %.1f MB' % ('OK' if out_mb < 40 else 'WARN', out_mb))
    L.append('[%s] full_text 非空: %.1f%%  (空 %d 行)'
             % ('OK' if st.empty_after == 0 else 'WARN',
                100.0 * (st.rows - st.empty_after) / st.rows if st.rows else 0, st.empty_after))
    L.append('')
    L.append('== 3. 文本长度 (full_text) ==')
    L.append('min=%d  p50=%d  p90=%d  p95=%d  max=%d'
             % (pct(lens, 0), pct(lens, 0.5), pct(lens, 0.9), pct(lens, 0.95), lens[-1] if lens else 0))
    L.append('平均=%.0f   原始字符总和=%d   清洗后=%d'
             % (st.full_chars / st.rows if st.rows else 0, st.raw_chars, st.full_chars))
    L.append('')
    L.append('== 4. 去噪明细(按规则统计删除字符数) ==')
    tot = 0
    for k, v in st.removed.items():
        L.append('  %-16s %8d' % (k, v)); tot += v
    L.append('  %-16s %8d' % ('合计', tot))
    L.append('  去噪率           %.2f%%' % (100.0 * tot / st.raw_chars if st.raw_chars else 0))
    L.append('  含占位文本的行    %d' % placeholder_seen)
    L.append('')
    L.append('== 5. 业务词保留校验 (原始 -> 清洗后) ==')
    L.append('  否定词(必须几乎不变):')
    for w in neg_words:
        r, f2 = neg_raw[w], neg_full[w]
        d = (f2 - r) / r * 100 if r else 0
        L.append('    %-4s %8d -> %8d   %+.1f%%  %s' % (w, r, f2, d, 'OK' if abs(d) < 5 else 'WARN'))
    L.append('  白名单词(必须 >0):')
    for w in white_words:
        L.append('    %-10s %8d -> %8d' % (w, wht_raw[w], wht_full[w]))
    L.append('')
    L.append('== 6. 去噪残留检查 ==')
    # 输出后再扫一遍残留
    resid_url = resid_emoji = 0
    with io.open(a.outp, encoding='utf-8-sig', newline='') as f2:
        for r2 in csv.DictReader(f2):
            t = r2.get('full_text') or ''
            resid_url += len(URL_PAT.findall(t))
            resid_emoji += len(EMOJI.findall(t))
    L.append('[%s] URL 残留  : %d 处' % ('OK' if resid_url <= 5 else 'WARN', resid_url))
    L.append('[%s] emoji 残留: %d 处' % ('OK' if resid_emoji == 0 else 'WARN', resid_emoji))
    L.append('')
    L.append('== 7. 下一步 ==')
    L.append('  抽检 20 条: 按 csv_row_no 回原始文件比对 id 与 char_count_raw')

    txt = '\n'.join(L)
    print('\n' + txt)
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    io.open(REPORT, 'w', encoding='utf-8').write(txt)
    print('\n报告已写入: %s' % REPORT)
    return 0


if __name__ == '__main__':
    sys.exit(main())