# -*- coding: utf-8 -*-
"""H / Speculative 的第二阶段动态筛选规则（纯函数，便于离线测试）。"""


def noul_value(ans):
    if not isinstance(ans, dict):
        return None
    v = ans.get('noul', ans.get('probability', ans.get('prob')))
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _select_h(payload, prior_answers):
    cfg = (payload.get('meta') or {}).get('flow') or {}
    questions = payload.get('questions') or {}
    gate = prior_answers.get('gate_cat') or {}
    fallback = cfg.get('fallback', 'all')
    info = {'type': 'h_leaf_filter', 'before': len(questions)}

    if not gate:
        use_all = fallback == 'all'
        selected = dict(questions) if use_all else {}
        info.update({'after': len(selected), 'fallback_used': fallback,
                     'reason': '类别阶段尚无成功响应'})
        return selected, info

    scored = []
    selected_cats = set()
    tau = float(cfg.get('cat_tau', 0.15))
    for qid, ans in gate.items():
        if not qid.startswith('cat_'):
            continue
        p = noul_value(ans)
        if p is None:
            continue
        cat = qid[4:]
        scored.append((p, cat))
        if p >= tau:
            selected_cats.add(cat)

    top_k = int(cfg.get('cat_k', 12))
    if top_k > 0:
        selected_cats.update(cat for _, cat in sorted(scored, reverse=True)[:top_k])

    qid_cats = cfg.get('qid_cats') or {}
    selected = {qid: q for qid, q in questions.items()
                if qid_cats.get(qid) in selected_cats}
    info.update({'after': len(selected), 'fallback_used': '',
                 'selected_categories': sorted(selected_cats),
                 'cat_tau': tau, 'cat_k': top_k})
    return selected, info


def _select_speculative(payload, prior_answers):
    cfg = (payload.get('meta') or {}).get('flow') or {}
    questions = payload.get('questions') or {}
    first = prior_answers.get('pass1') or {}
    fallback = cfg.get('fallback', 'none')
    info = {'type': 'speculative_pass2_filter', 'before': len(questions)}

    if not first:
        selected = dict(questions) if fallback == 'all' else {}
        info.update({'after': len(selected), 'fallback_used': fallback,
                     'reason': '第一轮尚无成功响应'})
        return selected, info

    lo, hi = float(cfg.get('lo', 0.2)), float(cfg.get('hi', 0.8))
    selected = {}
    for qid, question in questions.items():
        p = noul_value(first.get(qid))
        if p is not None and lo <= p <= hi:
            selected[qid] = question
    info.update({'after': len(selected), 'fallback_used': '', 'lo': lo, 'hi': hi})
    return selected, info


def apply_dynamic_filter(payload, prior_answers):
    """按方案/阶段裁问题。其他方案原样返回。

    prior_answers: {stage: {qid: answer}}，只含当前样本本次客户端运行已成功的响应。
    返回 (questions, selection_info)。空 questions 表示本请求应跳过，不发 API。
    """
    scheme = payload.get('scheme')
    stage = payload.get('stage', 'leaf')
    if scheme == 'H' and stage == 'leaf':
        return _select_h(payload, prior_answers)
    if scheme == 'Speculative' and stage == 'pass2':
        return _select_speculative(payload, prior_answers)
    return dict(payload.get('questions') or {}), {'type': 'none', 'before': len(payload.get('questions') or {}),
                                                   'after': len(payload.get('questions') or {})}
