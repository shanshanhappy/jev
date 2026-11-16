# -*- coding: utf-8 -*-
"""build_lexicon.py  标签体系词库 CSV -> data/label_lexicon.json
主键: idx = 词库数据行号(1基, 不含表头)。金标准表 词库原始行号 = idx + 1。
"""
import csv, json, os, re, sys
sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC  = os.path.join(ROOT, "data", "标签体系 20261113 - 【电商+社媒】细分维度词库.csv")
OUT  = os.path.join(ROOT, "data", "label_lexicon.json")
SEP  = re.compile(r"[、,，;；]")

def words(s):
    s = (s or "").strip()
    return [w.strip() for w in SEP.split(s) if w.strip()] if s else []

rows = list(csv.reader(open(SRC, encoding="utf-8-sig")))
body = [r for r in rows[1:] if r and r[0].strip()]
labels, dims = [], {}
for i, r in enumerate(body, start=1):
    r = r + [""] * (22 - len(r))
    near = r[8].strip()
    lab = dict(idx=i, cat=r[0].strip(), sub=r[1].strip(), label=r[2].strip(),
               hits=words(r[3]), ands=words(r[4]), excls=words(r[5]),
               poss=words(r[6]), negs=words(r[7]),
               near=int(near) if near.isdigit() else 15)
    labels.append(lab)
    dims.setdefault(lab["cat"], []).append(i)

json.dump(dict(source=os.path.basename(SRC), n_labels=len(labels), n_dims=len(dims),
               note="金标准.词库原始行号 = idx + 1", labels=labels, dims=dims),
          open(OUT, "w", encoding="utf-8"), ensure_ascii=False)

print("词库 -> %s" % OUT)
print("维度数=%d  标签数=%d" % (len(dims), len(labels)))
big = sorted(dims.items(), key=lambda kv: -len(kv[1]))[:10]
print("最大维度TOP10:", ", ".join("%s(%d)" % (k, len(v)) for k, v in big))
nohit = [l["idx"] for l in labels if not l["hits"]]
print("无命中关键词的行(词库粗筛模式将跳过):", nohit or "无")
mx = max(len(l["hits"]) for l in labels)
print("单词最多关键词数:", mx, " 平均:", round(sum(len(l["hits"]) for l in labels)/len(labels), 1))

try:
    sys.path.insert(0, os.path.join(ROOT, "core"))
    import genlog
    genlog.record("tools/build_lexicon.py", "生成词库", OUT, "file", len(labels),
                  note="源头词库 CSV 结构化；改完必须重新生成 payload",
                  params={"源文件": os.path.basename(SRC), "概念数": len(labels),
                          "维度数": len(dims)})
except Exception as _e:
    print("!! 生成日志写入失败(不影响结果): %s" % _e)