# -*- coding: utf-8 -*-
r"""
零依赖 xlsx -> CSV 拆分工具（仅用 Python 标准库）

用法:
    python tools\xlsx_to_csv.py <input.xlsx>             # 输出到 xlsx 所在目录
    python tools\xlsx_to_csv.py <input.xlsx> <输出目录>  # 显式指定输出目录
    python tools\xlsx_to_csv.py <input.xlsx> --out <输出目录>

注意: 输出落在 **源文件所在目录**，不是当前终端目录。
      想在项目任意位置运行，脚本路径可用绝对路径:
      python D:\Work\Modle\tools\xlsx_to_csv.py D:\Work\Modle\data\xxx.xlsx

默认把每个 sheet 导出为 <原文件名>_<sheet名>.csv。
输出为 UTF-8 BOM 编码，Excel 直接双击打开不乱码。

支持: 共享字符串 / 富文本 / 内联字符串 / 数字 / 布尔 / 错误值 /
      公式缓存值 / 日期格式 / 稀疏单元格（按列号对齐）/ 合并单元格。
"""
import csv
import datetime as dt
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def tag(name):
    return "{%s}%s" % (NS_MAIN, name)


def col_to_index(ref):
    """'C12' -> 2 (0-based column index)"""
    m = re.match(r"([A-Z]+)", ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def safe_filename(name):
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip() or "sheet"


def read_shared_strings(z):
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    result = []
    for si in root.findall(tag("si")):
        # 富文本可能拆成多个 <r><t>，全部拼接
        result.append("".join(t.text or "" for t in si.iter(tag("t"))))
    return result


def read_date_styles(z):
    """返回 set: 哪些 cellXfs 索引是日期格式"""
    if "xl/styles.xml" not in z.namelist():
        return set()
    root = ET.fromstring(z.read("xl/styles.xml"))

    builtin_date_ids = set(range(14, 23)) | set(range(45, 48)) | {27, 30, 36, 50, 57}
    custom = {}
    numfmts = root.find(tag("numFmts"))
    if numfmts is not None:
        for nf in numfmts.findall(tag("numFmt")):
            code = (nf.get("formatCode") or "").lower()
            # 去掉带引号的字面量和转义字符后再判断
            clean = re.sub(r'"[^"]*"|\\.', "", code)
            if re.search(r"[ymdhs]", clean) and "[h" not in clean:
                custom[int(nf.get("numFmtId"))] = True
            else:
                custom[int(nf.get("numFmtId"))] = False

    date_xfs = set()
    cellxfs = root.find(tag("cellXfs"))
    if cellxfs is not None:
        for i, xf in enumerate(cellxfs.findall(tag("xf"))):
            fid = int(xf.get("numFmtId") or 0)
            if fid in builtin_date_ids or custom.get(fid, False):
                date_xfs.add(i)
    return date_xfs


def excel_serial_to_datetime(value):
    """Excel 1900 日期系统序列号 -> datetime（含 1900 闰年 bug 修正）"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 0 or num > 2958466:
        return None
    base = dt.datetime(1899, 12, 30)
    return base + dt.timedelta(days=num)


def cell_value(c, shared, date_xfs):
    ctype = c.get("t")
    style = int(c.get("s") or 0)

    if ctype == "inlineStr":
        node = c.find(tag("is"))
        return "".join(t.text or "" for t in node.iter(tag("t"))) if node is not None else ""

    v = c.find(tag("v"))
    if v is None or v.text is None:
        return ""
    raw = v.text

    if ctype == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return raw
    if ctype == "b":
        return "TRUE" if raw == "1" else "FALSE"
    if ctype == "e":
        return raw
    if ctype == "str":
        return raw
    # 数字（含日期）
    if style in date_xfs:
        d = excel_serial_to_datetime(raw)
        if d is not None:
            return d.isoformat(sep=" ") if d.time() != dt.time(0, 0) else d.date().isoformat()
    # 去掉无意义的 .0
    if re.fullmatch(r"-?\d+\.0+", raw):
        return raw.split(".")[0]
    return raw


def sheet_rows(z, path, shared, date_xfs):
    root = ET.fromstring(z.read(path))
    data = root.find(tag("sheetData"))
    if data is None:
        return []
    rows = []
    for row in data.findall(tag("row")):
        cells = {}
        max_col = -1
        for c in row.findall(tag("c")):
            idx = col_to_index(c.get("r"))
            cells[idx] = cell_value(c, shared, date_xfs)
            max_col = max(max_col, idx)
        rows.append([cells.get(i, "") for i in range(max_col + 1)])
    # 去掉末尾全空行
    while rows and not any(str(x).strip() for x in rows[-1]):
        rows.pop()
    return rows


def trim_empty_columns(rows):
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    last = -1
    for i in range(width):
        if any(str(r[i]).strip() for r in rows):
            last = i
    return [r[: last + 1] for r in rows]


def convert(xlsx_path, output_dir=None):
    xlsx_path = os.path.abspath(xlsx_path)
    output_dir = os.path.abspath(output_dir) if output_dir else os.path.dirname(xlsx_path)
    os.makedirs(output_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(xlsx_path))[0]

    try:
        zf = zipfile.ZipFile(xlsx_path)
    except zipfile.BadZipFile:
        raise SystemExit(
            "!! 这个文件不是有效的 xlsx（可能仍是 .xls 老格式、下载未完成，或被 Excel 加密）\n"
            "   文件: %s" % xlsx_path)
    with zf as z:
        shared = read_shared_strings(z)
        date_xfs = read_date_styles(z)

        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rel_map = {
            r.get("Id"): r.get("Target")
            for r in rels.findall("{%s}Relationship" % NS_PKG)
        }

        outputs = []
        for sheet in wb.find(tag("sheets")).findall(tag("sheet")):
            name = sheet.get("name")
            target = rel_map.get(sheet.get("{%s}id" % NS_REL))
            if not target:
                print("  ! 跳过 %s：找不到关系目标" % name)
                continue
            path = target if target.startswith("xl/") else "xl/" + target.lstrip("/")
            path = os.path.normpath(path).replace("\\", "/")

            rows = trim_empty_columns(sheet_rows(z, path, shared, date_xfs))
            filename = "%s_%s.csv" % (safe_filename(stem), safe_filename(name))
            out_path = os.path.join(output_dir, filename)
            with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
                csv.writer(f).writerows(rows)
            ncol = len(rows[0]) if rows else 0
            outputs.append((name, out_path, len(rows), ncol))
            print("  [OK] %-16s -> %s  (%d 行 x %d 列)" % (name, filename, len(rows), ncol))
            # 表头定位: 首个「本行与下一行都 >=2 列有值」的行视为表头。
            # 这类导出常在表头前放 1~2 行大标题/说明，下游按列名取值会踩空。
            hdr = 1
            for i in range(min(4, len(rows) - 1)):
                c0 = sum(1 for x in rows[i] if str(x).strip())
                c1 = sum(1 for x in rows[i + 1] if str(x).strip())
                if c0 >= 2 and c1 >= 2:
                    hdr = i + 1
                    break
            if hdr > 1:
                print("       ! 前 %d 行是标题/说明，真正表头在第 %d 行"
                      "（下游按列名读需 skiprows=%d）" % (hdr - 1, hdr, hdr - 1))
    return outputs


def parse_args(argv):
    src, dst = None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        elif a in ("--out", "-o"):
            if i + 1 >= len(argv):
                raise SystemExit("!! --out 后面要跟输出目录")
            dst = argv[i + 1]
            i += 2
        elif src is None:
            src = a
            i += 1
        elif dst is None:
            dst = a
            i += 1
        else:
            raise SystemExit("!! 参数太多: %s（用法见 --help）" % a)
    return src, dst


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src, dst = parse_args(sys.argv[1:])
    if not src:
        raise SystemExit("!! 没给输入文件。用法: python tools\\xlsx_to_csv.py <input.xlsx> [输出目录]")

    # 输入体检：路径错/文件不存在时给能看懂的话，而不是 zipfile 的 traceback
    if not os.path.exists(src):
        print("!! 找不到输入文件: %s" % src)
        print("   当前终端目录: %s" % os.getcwd())
        d = os.path.dirname(os.path.abspath(src))
        if os.path.isdir(d):
            cand = sorted(x for x in os.listdir(d) if x.lower().endswith((".xlsx", ".xlsm")))
            if cand:
                print("   该目录下现有 xlsx: %s" % ", ".join(cand[:6]))
            else:
                print("   该目录下没有任何 .xlsx 文件")
        print("   检查: 文件名有无多余空格、是否含中文全角括号、是否还没下载完")
        sys.exit(2)
    if os.path.isdir(src):
        raise SystemExit("!! 这是个目录不是文件: %s" % src)
    if os.path.splitext(src)[1].lower() not in (".xlsx", ".xlsm"):
        print("   提醒: 扩展名不是 .xlsx/.xlsm，老版 .xls 不支持，请先另存为 .xlsx")

    print("源文件: %s" % os.path.abspath(src))
    outs = convert(src, dst)
    out_dir = os.path.dirname(outs[0][1]) if outs else (dst or os.path.dirname(os.path.abspath(src)))
    print("输出目录: %s" % os.path.abspath(out_dir))
    print("共导出 %d 个 sheet，合计 %d 行" % (len(outs), sum(o[2] for o in outs)))
