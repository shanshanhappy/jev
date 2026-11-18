# 脚本手册（tools=辅助处理 / core=方案④核心逻辑）

> 全部纯 Python 标准库（≥3.10），在项目根目录执行。数据口径见 `docs\数据处理方案.md`，打标方案见 `docs\方案4_Jev打标方案.md`；测试草案见 `docs\方案4_Jev测试方案_草案.md`。

## 目录分工

| 目录 | 内容 |
| --- | --- |
| `core\` | **核心逻辑**：`schemes.py`(造请求·7方案统一入口) `jev_client.py`(调API) `jev_eval.py`(解析/评测/交付表) `jev_rules.py`(共享规则层：证据回搜+情绪near，五方案公用) `genlog.py`(生成日志：文件说明书+快照+记录) |
| `tools\` | **辅助处理**：`build_clean.py`(清洗) `build_lexicon.py`(词库) `make_sample_list.py`(抽样清单) `refactor_gold.py`(金标准体检) `normalize_gold.py`(金标准归一+去重) `csv_view.py`(单行化预览) `xlsx_to_csv.py`(xlsx拆分，输出落 xlsx 同目录；支持 `--out`) |

## 流水线

```
tools\build_clean.py       data_export.csv ──▶ data_export_clean_v3.csv(19列) + docs\clean_report.txt(16项校验)
tools\build_lexicon.py     词库csv ──▶ data\label_lexicon.json(678标签/41维/行号主键)
tools\build_taxonomy.py    原始词库只读 ──▶ 标签体系_Jev整理版.csv + 标签体系_原始行映射.csv
tools\make_sample_list.py  抽样账本 ──▶ data\抽样清单.csv(样本号/行号/入样原因)
tools\refactor_gold.py     金标准 ──▶ 校验报告+行号补全表+17/12列空模板(只读不改写)
core\schemes.py            v3+词库+清单 ──▶ out\payloads_{方案}\{样本}__{方案}__{问法}.jsonl + .text.txt + .meta.json
core\jev_client.py         payloads ──▶ out\raw\{样本}__{模式}.jsonl($env:JEV_API_KEY; 默认官方端点)
core\jev_eval.py           raw+金标准 ──▶ 打标结果{细分-Jev 17列|汇总-Jev 12列} + 评测报告_{模式}.md
```

## 各脚本速查

| 脚本 | 一条典型命令 | 关键参数/注意 |
| --- | --- | --- |
| `tools\build_clean.py` | `python tools\build_clean.py`（默认 原始 → v3，无需带参） | 默认输入 `data\data_export.csv`(47列)、输出 `data\data_export_clean_v3.csv`；`--limit N` 试跑；`--no-denoise` 对照；content/topics 也去噪(v1.3) |
| `tools\build_lexicon.py` | `python tools\build_lexicon.py` | 幂等；词库更新后重跑 |
| `tools\build_taxonomy.py` | `python tools\build_taxonomy.py` | 原始词库不动，生成整理版645概念与原始行映射678行 |
| `tools\make_sample_list.py` | `python tools\make_sample_list.py` | 生产批用 `--src 抽样记录.csv --out data\抽样清单_批2.csv` |
| `tools\refactor_gold.py` | `python tools\refactor_gold.py` | 金标准新版本后重跑；防同文件读写 |
| `tools\normalize_gold.py` | `python tools\normalize_gold.py --in-place` | 金标准行号归一（词库同概念多行→组代表）+ 同(样本,标签,对象)去重；`--dry-run` 先看统计 |
| `core\schemes.py` | `python core\schemes.py --scheme A2 --outdir out\payloads_A2` | 7 方案统一入口，一次跑一个方案；A2=638问≈63.8k tok(实测)；B=粗筛≈3k；`--max-chars 2000 --budget 55000` |
| `core\jev_client.py` | `python core\jev_client.py --dir out\payloads --samples S001 --dry-run` | 先 `--dry-run`；`$env:JEV_BASE_URL` 换端点；429/5xx 指数退避；中转注意 1.13 版本差 |
| `core\jev_eval.py` | `python core\jev_eval.py --mode A2 --tau 0.5` | 灰区 `--tau-lo 0.3 --tau-hi 0.7`；金标准=答卷只在此读取；重名列表行任一命中 |
| `tools\csv_view.py` | `python tools\csv_view.py data\data_export_clean_v3.csv -n 20 --cols csv_row_no,full_text` | 内部换行显示为⏎；`--flat` 出阅读副本(勿回灌) |
| `core\genlog.py` | `python core\genlog.py` | **不认识 out\ 里哪个文件就跑这个**：文件说明书+当前快照+最近记录；`--tail N` / `--snapshot` / `--doctor` 自检 / `--clear --yes` |

## 通用红线

1. 原始文件只读；输出永远新文件；**任何 CSV 禁 Excel 另存**（毁长ID）；
2. `out\payloads\` 在 prompts→client→eval 之间不可清空（评测依赖 text/meta）；
3. 密钥只走 `JEV_API_KEY` 环境变量；
4. 金标准 v1 是 LLM 初稿：一切"准确率"在 T1 阶段只能叫**模型间一致率**，95% 结论等人审定稿版。