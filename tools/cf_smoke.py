#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Cloudflare Workers AI 上的 Jev 模型冒烟测试（零依赖，标准库实现）

用途
----
1. 验证 CF_ACCOUNT_ID / CF_API_TOKEN 是否配置正确
2. 观察 Jev 真实响应结构（choice / noul / score 三类）
3. 确认免费额度是否在扣（看返回的 usage.input_tokens）

用法
----
    # 1) 设置环境变量（Git Bash / CMD / PowerShell 均适用各自语法）
    export CF_ACCOUNT_ID="你的32位AccountID"
    export CF_API_TOKEN="你的Workers AI Token"

    # 2) 运行
    python tools/cf_smoke.py

    # 也可以单行传参（不依赖环境变量）
    python tools/cf_smoke.py --account-id xxx --token yyy

注意
----
- Cloudflare 侧 Jev 的 context 上限是 32,000 tokens（官方为 64k），
  本项目 A2 方案单帖约 48,449 tokens，会超限。本脚本只发很小的请求。
- 响应是【直接返回】的 {model, answers, usage}，没有 {"result": ..., "success": true} 包裹。
  这与 /ai/run/{model} 的老式模型不同，不要照抄 llama 的解析代码。
- 实际模型版本是 jev-1.13.0（响应 model 字段会如实返回），与官方 1.14.0 不同。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run"
MODEL = "typesafe/jev"
TIMEOUT = 60

# 用一条短文本，避免触发 32k 上限
SAMPLE_STATE = "这款面霜用了两周，脸上的斑淡了一点，但是有点搓泥，质地偏厚。"

# 三类问题各来一个，一次性验证响应结构
SAMPLE_QUESTIONS = {
    "is_negative": {
        "type": "noul",
        "instructions": "这段内容是否包含负面评价？",
    },
    "sentiment": {
        "type": "choice",
        "instructions": "整体情感倾向是？",
        "criteria": {
            "正面": "以肯定、推荐为主",
            "中性": "客观描述，无明显倾向",
            "负面": "以抱怨、不推荐为主",
        },
    },
    "effect_score": {
        "type": "score",
        "instructions": "对产品功效的认可程度如何？",
        "criteria": ["完全不认可", "部分认可", "比较认可", "非常认可"],
    },
}


def build_request(account_id, token, state, questions):
    """构造 POST 请求。Cloudflare 需要把 state/questions 包在 input 里。"""
    body = json.dumps(
        {"model": MODEL, "input": {"state": state, "questions": questions}},
        ensure_ascii=False,
    ).encode("utf-8")

    return urllib.request.Request(
        ENDPOINT.format(account_id=account_id),
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )


def call(account_id, token, state=SAMPLE_STATE, questions=None, verbose=True):
    """
    调用 Jev，返回 (ok, data)。

    ok=True 时 data 是模型返回体；ok=False 时 data 是错误信息字符串。
    响应若被 {"result": ...} 包裹会自动解包（兼容两种端点形态）。
    """
    questions = questions or SAMPLE_QUESTIONS
    req = build_request(account_id, token, state, questions)

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        hint = ""
        if e.code == 401:
            hint = " → Token 无效。检查是否复制完整、权限是否为 Workers AI - Read + Edit"
        elif e.code == 403:
            hint = " → 权限不足。用模板建的 Token 默认可能只有 Read，需改成 Edit"
        elif e.code == 429:
            hint = " → 触发限流。免费层 10,000 Neurons/天，可能已用尽（UTC 0 点重置）"
        elif e.code == 400:
            hint = " → 请求体被拒。常见原因：state 超过 32k context、questions 结构不符合 schema"
        return False, "HTTP %d%s\n%s" % (e.code, hint, detail)
    except urllib.error.URLError as e:
        return False, "网络错误（国内直连 api.cloudflare.com 通常可达）：%s" % e.reason
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)

    # 兼容：有的端点会包一层 {"result": {...}, "success": true}
    if isinstance(raw, dict) and "result" in raw and isinstance(raw["result"], dict):
        raw = raw["result"]

    if verbose:
        print(json.dumps(raw, ensure_ascii=False, indent=2))

    return True, raw


def summarize(data):
    """把 answers 压成一行结论，方便快速判断是否跑通。"""
    lines = []
    for key, ans in (data.get("answers") or {}).items():
        t = ans.get("type")
        if t == "choice":
            lines.append("  %-14s choice=%s  confidence=%.3f" % (key, ans.get("choice"), ans.get("confidence", 0)))
        elif t == "noul":
            lines.append("  %-14s noul=%.3f" % (key, ans.get("noul", 0)))
        elif t == "score":
            lines.append("  %-14s score=%.2f  confidence=%.3f" % (key, ans.get("score", 0), ans.get("confidence", 0)))
        else:
            lines.append("  %-14s %s" % (key, json.dumps(ans, ensure_ascii=False)))
    usage = data.get("usage") or {}
    lines.append("  模型版本: %s | input_tokens=%s output_tokens=%s"
                 % (data.get("model", "?"), usage.get("input_tokens", "?"), usage.get("output_tokens", "?")))
    return "\n".join(lines)


def main():
    # Windows 控制台默认 GBK，中文输出会抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    ap = argparse.ArgumentParser(description="Cloudflare Workers AI 上的 Jev 冒烟测试")
    ap.add_argument("--account-id", default=os.environ.get("CF_ACCOUNT_ID"), help="Cloudflare Account ID（32 位）")
    ap.add_argument("--token", default=os.environ.get("CF_API_TOKEN"), help="Workers AI API Token")
    ap.add_argument("--state", default=SAMPLE_STATE, help="自定义待分类文本")
    ap.add_argument("--raw", action="store_true", help="只打印原始响应，不做摘要")
    args = ap.parse_args()

    if not args.account_id or not args.token:
        print("缺少凭证。请设置环境变量：")
        print("    export CF_ACCOUNT_ID=\"你的32位AccountID\"")
        print("    export CF_API_TOKEN=\"你的Token\"")
        print("或使用参数：python tools/cf_smoke.py --account-id xxx --token yyy")
        return 2

    print("端点: %s" % ENDPOINT.format(account_id=args.account_id[:6] + "..." + args.account_id[-4:]))
    print("模型: %s" % MODEL)
    print("state: %s" % args.state[:60])
    print("-" * 70)

    ok, data = call(args.account_id, args.token, state=args.state, verbose=args.raw)

    if not ok:
        print("[失败] %s" % data)
        return 1

    print("[成功] 链路已通，额度可用")
    print(summarize(data))
    print("-" * 70)
    print("提醒：Cloudflare 侧 context 上限 32k，A2 方案（约 48k）需压缩后才能在此运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
