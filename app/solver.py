"""Exam-paper composition solver built on OR-Tools CP-SAT.

支持两种模式（同一套数据整理与求解参数）：

单卷（paper_count 缺省或为 1）：
  择优规则（字典序，逐层最优后固定再优化下一层）：
    1. 难度偏差 |平均难度 - 目标难度| 最小；
    2. 重复知识点数（命中 >=2 道题的知识点个数）最少；
    3. 题目编号序列最小（按编号规范化排序后的 0/1 选择向量，分块编码）。

多卷平行卷（paper_count = 2~5）：
  所有试卷放入同一个联合 CP-SAT 模型，x[p][i] 表示第 p 份卷是否选第 i 题；
  每份卷分别满足总分、题型、知识点覆盖、难度区间、互斥约束，
  并对任意两份卷约束共同题数 <= max_shared_questions。
  择优规则：
    1. 各卷难度偏差总和最小；
    2. 跨卷重复题总数（Σ_{a<b}|卷a ∩ 卷b|）最少；
    3. 各卷题号序列最小（按卷序拼接的 0/1 选择向量，分块编码）。

无解时通过“逐项删减验证”求删除极小的冲突约束集（deletion-based MUS）：
逐条尝试移除约束，若移除后仍无解则永久删除，最终保留的即为最小冲突集。
多卷模式下冲突项带 scope=paper/cross 与 paper 序号，区分单卷约束与跨卷重叠上限。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

SCALE = 1000  # 难度浮点 -> 整数的放大倍数
CHUNK_BITS = 20  # 编号序列分块编码的块大小，2^20 远在 int64 安全范围内
LEX_CHUNK_BITS = 62  # 多卷阶段 3 的字典序分块：2^62 仍在 CP-SAT int64 安全范围内
STAGE_TIME_LIMIT = 20.0  # 每个择优阶段的求解上限（秒）
MUS_TIME_LIMIT = 15.0  # 冲突集逐项验证的单次求解上限（秒）


# --------------------------------------------------------------------------- #
# 数据整理
# --------------------------------------------------------------------------- #
def _id_sort_key(qid: Any):
    """编号规范化排序：整数型编号在前按数值排，其余按字符串排。"""
    if isinstance(qid, bool):  # guard: bool 是 int 的子类
        return (1, str(qid))
    if isinstance(qid, int):
        return (0, qid)
    s = str(qid)
    if s.lstrip("-").isdigit():
        return (0, int(s))
    return (1, s)


def _prepare(req: Dict[str, Any]) -> Dict[str, Any]:
    questions = sorted(req["questions"], key=lambda q: _id_sort_key(q["id"]))
    ids = [q["id"] for q in questions]
    id_index = {qid: i for i, qid in enumerate(ids)}

    type_members: Dict[str, List[int]] = {}
    kp_members: Dict[str, List[int]] = {}
    for i, q in enumerate(questions):
        type_members.setdefault(q["type"], []).append(i)
        for kp in set(q["knowledge_points"]):
            kp_members.setdefault(kp, []).append(i)

    lo, hi = req["difficulty_range"]
    target = req["target_difficulty"]
    if target is None:
        target = (lo + hi) / 2.0

    # 只保留非平凡的互斥题组（成员均存在且 >=2）
    mutex_groups = [g for g in req.get("mutex_groups", []) if len(set(g)) >= 2]

    paper_count = req.get("paper_count")
    P = int(paper_count) if paper_count else 1
    max_shared = req.get("max_shared_questions")
    if max_shared is None:
        max_shared = 0 if P >= 2 else None

    return {
        "questions": questions,
        "ids": ids,
        "id_index": id_index,
        "type_members": type_members,
        "kp_members": kp_members,
        "T": int(req["total_score"]),
        "type_counts": dict(req.get("type_counts", {})),
        "coverage": dict(req.get("knowledge_coverage", {})),
        "diff_lo": float(lo),
        "diff_hi": float(hi),
        "target": float(target),
        "mutex_groups": mutex_groups,
        "seed": int(req.get("seed", 42)),
        "P": P,
        "max_shared": None if max_shared is None else int(max_shared),
    }


# --------------------------------------------------------------------------- #
# 约束清单
# --------------------------------------------------------------------------- #
def _paper_specs(d: Dict[str, Any], p: Optional[int]) -> List[Dict[str, Any]]:
    """展开单份卷的约束项；p=None 表示单卷模式（不带 scope/paper 字段）。"""
    tag: Dict[str, Any] = {} if p is None else {"scope": "paper", "paper": p}
    specs: List[Dict[str, Any]] = [
        {**tag, "kind": "total_score", "detail": {"total_score": d["T"]}}
    ]
    for t, c in d["type_counts"].items():
        specs.append({**tag, "kind": "type", "detail": {"type": t, "count": int(c)}})
    for kp, c in d["coverage"].items():
        specs.append(
            {**tag, "kind": "coverage",
             "detail": {"knowledge_point": kp, "min_count": int(c)}}
        )
    specs.append(
        {**tag, "kind": "difficulty_range",
         "detail": {"range": [d["diff_lo"], d["diff_hi"]]}}
    )
    for g in d["mutex_groups"]:
        specs.append({**tag, "kind": "mutex", "detail": {"group": list(g)}})
    return specs


def _build_constraint_specs(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    """单卷模式：带编号、保留原始顺序的约束项。"""
    return _paper_specs(d, None)


def _build_multi_specs(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    """多卷模式：先按卷序号排列各卷约束，再排列跨卷重叠上限。"""
    specs: List[Dict[str, Any]] = []
    for p in range(d["P"]):
        specs.extend(_paper_specs(d, p))
    cap = d["max_shared"]
    for a in range(d["P"]):
        for b in range(a + 1, d["P"]):
            specs.append({
                "scope": "cross",
                "kind": "shared_limit",
                "detail": {"papers": [a, b], "max_shared_questions": cap},
            })
    return specs


# --------------------------------------------------------------------------- #
# 单卷建模
# --------------------------------------------------------------------------- #
def _build_model(
    d: Dict[str, Any], active: set
) -> Tuple[cp_model.CpModel, List[cp_model.IntVar], Dict[str, Any]]:
    m = cp_model.CpModel()
    n = len(d["questions"])
    x = [m.NewBoolVar(f"x_{i}") for i in range(n)]

    # 总分
    score_sum = sum(int(q["score"]) * x[i] for i, q in enumerate(d["questions"]))

    # 放大后的难度加权和 SD = Σ round(diff*1000) * score * x，平均难度 = SD / (1000*T)
    sd_terms = [int(round(q["difficulty"] * SCALE)) * int(q["score"]) * x[i]
                for i, q in enumerate(d["questions"])]
    sd = sum(sd_terms) if sd_terms else 0

    # 难度偏差（相对目标平均难度），SCALE 单位下的整数变量
    target_scaled = int(round(d["target"] * SCALE * d["T"]))
    max_dev = max(abs(int(round(d["diff_hi"] * SCALE * d["T"])) - target_scaled),
                  abs(target_scaled - int(round(d["diff_lo"] * SCALE * d["T"]))),
                  SCALE * d["T"])
    dev = m.NewIntVar(0, max_dev, "dev")
    m.Add(dev >= sd - target_scaled)
    m.Add(dev >= target_scaled - sd)

    extra = {"score_sum": score_sum, "sd": sd, "dev": dev}

    specs = d["specs"]
    for cid in active:
        spec = specs[cid]
        kind = spec["kind"]

        if kind == "total_score":
            m.Add(score_sum == d["T"])

        elif kind == "type":
            t = spec["detail"]["type"]
            c = spec["detail"]["count"]
            members = d["type_members"].get(t, [])
            m.Add(sum(x[i] for i in members) == c)

        elif kind == "coverage":
            kp = spec["detail"]["knowledge_point"]
            c = spec["detail"]["min_count"]
            members = d["kp_members"].get(kp, [])
            if not members:  # 知识点在题库快照中没有任何题目
                z = m.NewBoolVar(f"impossible_cov_{cid}")
                m.Add(z == 0)
                m.Add(z == 1)
            else:
                m.Add(sum(x[i] for i in members) >= c)

        elif kind == "difficulty_range":
            lb = int(round(d["diff_lo"] * SCALE * d["T"]))
            ub = int(round(d["diff_hi"] * SCALE * d["T"]))
            m.Add(sd >= lb)
            m.Add(sd <= ub)

        elif kind == "mutex":
            group = spec["detail"]["group"]
            m.Add(sum(x[d["id_index"][qid]] for qid in group) <= 1)

    return m, x, extra


# --------------------------------------------------------------------------- #
# 多卷联合建模
# --------------------------------------------------------------------------- #
def _build_multi_model(
    d: Dict[str, Any], active: set
) -> Tuple[cp_model.CpModel, List[List[cp_model.IntVar]], Dict[str, Any]]:
    m = cp_model.CpModel()
    n = len(d["questions"])
    P = d["P"]
    # x[p][i]：第 p 份卷是否选择第 i 题（i 按编号规范化排序）
    x = [[m.NewBoolVar(f"x_{p}_{i}") for i in range(n)] for p in range(P)]

    score_sums: List[Any] = []
    sds: List[Any] = []
    devs: List[cp_model.IntVar] = []
    target_scaled = int(round(d["target"] * SCALE * d["T"]))
    lb_scaled = int(round(d["diff_lo"] * SCALE * d["T"]))
    ub_scaled = int(round(d["diff_hi"] * SCALE * d["T"]))
    max_dev = max(abs(ub_scaled - target_scaled),
                  abs(target_scaled - lb_scaled),
                  SCALE * d["T"])

    for p in range(P):
        score_sums.append(
            sum(int(q["score"]) * x[p][i] for i, q in enumerate(d["questions"]))
        )
        sd_terms = [int(round(q["difficulty"] * SCALE)) * int(q["score"]) * x[p][i]
                    for i, q in enumerate(d["questions"])]
        sds.append(sum(sd_terms) if sd_terms else 0)
        dev = m.NewIntVar(0, max_dev, f"dev_{p}")
        m.Add(dev >= sds[p] - target_scaled)
        m.Add(dev >= target_scaled - sds[p])
        devs.append(dev)

    # 跨卷共同题：pair[(a,b)][i] = x[a][i] AND x[b][i]；无条件预建，仅在
    # 对应 shared_limit 约束生效时加入 <= cap 限制（MUS 逐项删减时需要）。
    shared_counts: Dict[Tuple[int, int], cp_model.IntVar] = {}
    for a in range(P):
        for b in range(a + 1, P):
            pair = [m.NewBoolVar(f"y_{a}_{b}_{i}") for i in range(n)]
            for i in range(n):
                m.Add(pair[i] <= x[a][i])
                m.Add(pair[i] <= x[b][i])
                m.Add(pair[i] >= x[a][i] + x[b][i] - 1)
            shared_counts[(a, b)] = sum(pair)

    total_dev = sum(devs)

    for cid in active:
        spec = d["specs"][cid]
        kind = spec["kind"]

        if kind == "shared_limit":
            a, b = spec["detail"]["papers"]
            cap = spec["detail"]["max_shared_questions"]
            m.Add(shared_counts[(a, b)] <= int(cap))
            continue

        p = spec["paper"]
        xp = x[p]

        if kind == "total_score":
            m.Add(score_sums[p] == d["T"])

        elif kind == "type":
            t = spec["detail"]["type"]
            c = spec["detail"]["count"]
            members = d["type_members"].get(t, [])
            m.Add(sum(xp[i] for i in members) == c)

        elif kind == "coverage":
            kp = spec["detail"]["knowledge_point"]
            c = spec["detail"]["min_count"]
            members = d["kp_members"].get(kp, [])
            if not members:  # 知识点在题库快照中没有任何题目
                z = m.NewBoolVar(f"impossible_cov_{cid}")
                m.Add(z == 0)
                m.Add(z == 1)
            else:
                m.Add(sum(xp[i] for i in members) >= c)

        elif kind == "difficulty_range":
            m.Add(sds[p] >= lb_scaled)
            m.Add(sds[p] <= ub_scaled)

        elif kind == "mutex":
            group = spec["detail"]["group"]
            m.Add(sum(xp[d["id_index"][qid]] for qid in group) <= 1)

    extra = {
        "devs": devs,
        "total_dev": total_dev,
        "shared_counts": shared_counts,
    }
    return m, x, extra


def _solver(d: Dict[str, Any]) -> cp_model.CpSolver:
    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 1  # 单线程保证确定性
    s.parameters.random_seed = d["seed"]
    return s


def _solve(
    d: Dict[str, Any],
    active: set,
    *,
    time_limit: float,
) -> Tuple[str, Optional[List[bool]]]:
    """单卷：在给定约束子集上求解，返回 (状态, 选择向量)。状态为 feasible/infeasible/unknown。"""
    m, x, _ = _build_model(d, active)
    s = _solver(d)
    s.parameters.max_time_in_seconds = time_limit
    status = s.Solve(m)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return "feasible", [bool(s.Value(x[i])) for i in range(len(x))]
    if status == cp_model.INFEASIBLE:
        return "infeasible", None
    return "unknown", None


def _solve_multi(
    d: Dict[str, Any],
    active: set,
    *,
    time_limit: float,
) -> Tuple[str, Optional[List[List[bool]]]]:
    """多卷：在给定约束子集上求解，返回 (状态, 各卷选择向量)。"""
    m, x, _ = _build_multi_model(d, active)
    s = _solver(d)
    s.parameters.max_time_in_seconds = time_limit
    status = s.Solve(m)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        P, n = d["P"], len(d["questions"])
        return "feasible", [[bool(s.Value(x[p][i])) for i in range(n)] for p in range(P)]
    if status == cp_model.INFEASIBLE:
        return "infeasible", None
    return "unknown", None


# --------------------------------------------------------------------------- #
# 快速无解预判（仅用于立即给出结论；冲突集仍以求解器逐项验证为准）
# --------------------------------------------------------------------------- #
def _quick_infeasible(d: Dict[str, Any], active: set) -> Optional[str]:
    specs = d["specs"]
    for cid in active:
        spec = specs[cid]
        kind, detail = spec["kind"], spec["detail"]
        if kind == "total_score":
            if sum(int(q["score"]) for q in d["questions"]) < d["T"]:
                return "题库总分不足，无法达到试卷总分"
        elif kind == "type":
            if len(d["type_members"].get(detail["type"], [])) < detail["count"]:
                return f"题型 {detail['type']} 的题量不足"
        elif kind == "coverage":
            if len(d["kp_members"].get(detail["knowledge_point"], [])) < detail["min_count"]:
                return f"知识点 {detail['knowledge_point']} 的题量不足"
    return None


def _quick_infeasible_multi(d: Dict[str, Any], active: set) -> Optional[str]:
    specs = d["specs"]
    P = d["P"]

    # 单卷维度的必要条件
    for cid in active:
        spec = specs[cid]
        if spec["scope"] != "paper":
            continue
        p, kind, detail = spec["paper"], spec["kind"], spec["detail"]
        prefix = f"第 {p + 1} 份卷："
        if kind == "total_score":
            if sum(int(q["score"]) for q in d["questions"]) < d["T"]:
                return prefix + "题库总分不足，无法达到试卷总分"
        elif kind == "type":
            if len(d["type_members"].get(detail["type"], [])) < detail["count"]:
                return prefix + f"题型 {detail['type']} 的题量不足"
        elif kind == "coverage":
            if len(d["kp_members"].get(detail["knowledge_point"], [])) < detail["min_count"]:
                return prefix + f"知识点 {detail['knowledge_point']} 的题量不足"

    # 跨卷维度：仅当所有跨卷重叠上限都生效且均为 0（各卷两两不相交）时，
    # 以下计数下界才是可靠的必要条件；任何一条被放宽都交给求解器判定。
    cross_ids = {cid for cid, sp in enumerate(specs) if sp["scope"] == "cross"}
    if cross_ids and cross_ids <= active:
        caps = {specs[cid]["detail"]["max_shared_questions"] for cid in cross_ids}
        if caps == {0}:
            n = len(d["questions"])
            active_score_papers = {
                specs[cid]["paper"] for cid in active
                if specs[cid]["scope"] == "paper" and specs[cid]["kind"] == "total_score"
            }
            if len(active_score_papers) == P:
                bank_total = sum(int(q["score"]) for q in d["questions"])
                if bank_total < P * d["T"]:
                    return (f"题库总分 {bank_total} 不足以在不共享题目的条件下组成 "
                            f"{P} 份总分 {d['T']} 的平行卷")
            for t, c in d["type_counts"].items():
                need = sum(
                    specs[cid]["detail"]["count"] for cid in active
                    if specs[cid]["scope"] == "paper" and specs[cid]["kind"] == "type"
                    and specs[cid]["detail"]["type"] == t
                )
                have = len(d["type_members"].get(t, []))
                if need > have:
                    return (f"题型 {t} 题库仅 {have} 题，不共享题目时各卷共需 {need} 题")
            for kp, c in d["coverage"].items():
                need = sum(
                    specs[cid]["detail"]["min_count"] for cid in active
                    if specs[cid]["scope"] == "paper" and specs[cid]["kind"] == "coverage"
                    and specs[cid]["detail"]["knowledge_point"] == kp
                )
                have = len(d["kp_members"].get(kp, []))
                if need > have:
                    return (f"知识点 {kp} 题库仅 {have} 题，不共享题目时各卷共需 {need} 题")
    return None


# --------------------------------------------------------------------------- #
# 分层择优（单卷）
# --------------------------------------------------------------------------- #
def _optimize(d: Dict[str, Any]) -> Tuple[str, Optional[List[bool]], Optional[str]]:
    """三层字典序择优，逐阶段固定已得到的最优值。"""
    n = len(d["questions"])
    active = set(range(len(d["specs"])))

    # 阶段 1：难度偏差最小
    m, x, extra = _build_model(d, active)
    m.Minimize(extra["dev"])
    s = _solver(d)
    s.parameters.max_time_in_seconds = STAGE_TIME_LIMIT
    status = s.Solve(m)
    if status == cp_model.INFEASIBLE:
        return "infeasible", None, None
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return "unknown", None, "求解器在规定时间内无法判定可行性"
    best_dev = int(s.Value(extra["dev"]))
    chosen = [bool(s.Value(x[i])) for i in range(n)]

    # 阶段 2：重复知识点数最少（固定难度偏差）
    m, x, extra = _build_model(d, active)
    m.Add(extra["dev"] == best_dev)
    repeat_vars = []
    for kp, members in d["kp_members"].items():
        if len(members) < 2:
            continue
        r = m.NewBoolVar(f"repeat_{kp}")
        cnt = sum(x[i] for i in members)
        m.Add(cnt >= 2).OnlyEnforceIf(r)
        m.Add(cnt <= 1).OnlyEnforceIf(r.Not())
        repeat_vars.append(r)
    repeats = sum(repeat_vars) if repeat_vars else 0
    m.Minimize(repeats)
    s = _solver(d)
    s.parameters.max_time_in_seconds = STAGE_TIME_LIMIT
    status = s.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # 退化：直接使用阶段 1 的解
        return "feasible", chosen, None
    best_repeats = int(s.Value(repeats)) if repeat_vars else 0
    chosen = [bool(s.Value(x[i])) for i in range(n)]

    # 阶段 3：题目编号序列最小（分块编码，逐块字典序最小化）
    n_chunks = (n + CHUNK_BITS - 1) // CHUNK_BITS
    fixed_chunks: List[Tuple[int, int]] = []
    for k in range(n_chunks):
        m, x, extra = _build_model(d, active)
        m.Add(extra["dev"] == best_dev)

        repeat_vars = []
        for kp, members in d["kp_members"].items():
            if len(members) < 2:
                continue
            r = m.NewBoolVar(f"repeat_{kp}")
            cnt = sum(x[i] for i in members)
            m.Add(cnt >= 2).OnlyEnforceIf(r)
            m.Add(cnt <= 1).OnlyEnforceIf(r.Not())
            repeat_vars.append(r)
        if repeat_vars:
            m.Add(sum(repeat_vars) == best_repeats)

        chunk_values = {}
        for ck, cv in fixed_chunks:
            lo, hi = ck * CHUNK_BITS, min((ck + 1) * CHUNK_BITS, n)
            m.Add(sum(x[lo + p] * (1 << p) for p in range(hi - lo)) == cv)

        lo, hi = k * CHUNK_BITS, min((k + 1) * CHUNK_BITS, n)
        e_k = sum(x[lo + p] * (1 << p) for p in range(hi - lo))
        m.Minimize(e_k)

        s = _solver(d)
        s.parameters.max_time_in_seconds = STAGE_TIME_LIMIT
        status = s.Solve(m)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break  # 保留上一阶段已得的最优解
        fixed_chunks.append((k, int(s.Value(e_k))))
        chosen = [bool(s.Value(x[i])) for i in range(n)]

    d["_best_dev"] = best_dev
    d["_best_repeats"] = best_repeats
    return "feasible", chosen, None


# --------------------------------------------------------------------------- #
# 分层择优（多卷联合）
# --------------------------------------------------------------------------- #
def _multi_optimize(
    d: Dict[str, Any],
) -> Tuple[str, Optional[List[List[bool]]], Optional[str]]:
    """多卷三层字典序择优：偏差总和 → 跨卷重复题总数 → 卷序拼接题号序列。"""
    P, n = d["P"], len(d["questions"])
    active = set(range(len(d["specs"])))

    # 阶段 1：各卷难度偏差总和最小
    m, x, extra = _build_multi_model(d, active)
    m.Minimize(extra["total_dev"])
    s = _solver(d)
    s.parameters.max_time_in_seconds = STAGE_TIME_LIMIT
    status = s.Solve(m)
    if status == cp_model.INFEASIBLE:
        return "infeasible", None, None
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return "unknown", None, "求解器在规定时间内无法判定可行性"
    best_dev_total = int(s.Value(extra["total_dev"]))
    chosen = [[bool(s.Value(x[p][i])) for i in range(n)] for p in range(P)]

    # 阶段 2：跨卷重复题总数最少（固定偏差总和）
    m, x, extra = _build_multi_model(d, active)
    m.Add(extra["total_dev"] == best_dev_total)
    total_shared = sum(extra["shared_counts"].values())
    m.Minimize(total_shared)
    s = _solver(d)
    s.parameters.max_time_in_seconds = STAGE_TIME_LIMIT
    status = s.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return "feasible", chosen, None  # 退化：使用阶段 1 的解
    best_shared = int(s.Value(total_shared))
    chosen = [[bool(s.Value(x[p][i])) for i in range(n)] for p in range(P)]

    # 阶段 3：卷序拼接的题号选择序列最小（分块编码，逐块字典序最小化）。
    # 固定前两层最优值后，逐块最小化即可得到唯一确定的选择向量。
    # 每块最多 62 位，权重 2^62 仍在 CP-SAT int64 范围内，单次求解即证最优。
    total_bits = P * n
    n_chunks = (total_bits + LEX_CHUNK_BITS - 1) // LEX_CHUNK_BITS
    fixed_chunks: List[Tuple[int, int]] = []

    for k in range(n_chunks):
        m, x, extra = _build_multi_model(d, active)
        m.Add(extra["total_dev"] == best_dev_total)
        m.Add(sum(extra["shared_counts"].values()) == best_shared)

        for ck, cv in fixed_chunks:
            terms = []
            for bit_pos in range(
                ck * LEX_CHUNK_BITS, min((ck + 1) * LEX_CHUNK_BITS, total_bits)
            ):
                p, i = divmod(bit_pos, n)
                terms.append(x[p][i] * (1 << (bit_pos - ck * LEX_CHUNK_BITS)))
            m.Add(sum(terms) == cv)

        lo = k * LEX_CHUNK_BITS
        hi = min((k + 1) * LEX_CHUNK_BITS, total_bits)
        e_k = sum(
            x[bit_pos // n][bit_pos % n] * (1 << (bit_pos - lo))
            for bit_pos in range(lo, hi)
        )
        m.Minimize(e_k)

        s = _solver(d)
        s.parameters.max_time_in_seconds = STAGE_TIME_LIMIT
        status = s.Solve(m)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            break  # 保留上一阶段已得的最优解
        fixed_chunks.append((k, int(s.Value(e_k))))
        chosen = [[bool(s.Value(x[p][i])) for i in range(n)] for p in range(P)]

    d["_best_dev_total"] = best_dev_total
    d["_best_shared"] = best_shared
    return "feasible", chosen, None


# --------------------------------------------------------------------------- #
# 最小冲突集（逐项删减验证）
# --------------------------------------------------------------------------- #
def _minimal_conflicts(d: Dict[str, Any]) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """Deletion-based MUS：逐条尝试移除，移除后仍无解则永久删除。

    最终保留下来的每条约束都是“必要”的（删去它其余约束即可满足），
    即关于删除操作极小的冲突约束集。

    单次判定若超时（unknown），保守视为“可能可行”，即不把该约束计入冲突集，
    避免输出未经严格证明的冲突项；此时整体结果标记为 incomplete。
    """
    active = set(range(len(d["specs"])))

    # 以完整约束集无解为前提
    note = "ok"
    reason = _quick_infeasible(d, active)
    if reason is None:
        status, _ = _solve(d, active, time_limit=MUS_TIME_LIMIT)
        if status == "unknown":
            return None, "求解器无法在限定时间内判定约束是否无解，未能给出冲突集"
        if status != "infeasible":
            return [], "ok"  # 实际上可行，不存在冲突集

    kept: List[int] = []
    for cid in range(len(d["specs"])):
        if cid not in active:
            continue
        trial = active - {cid}
        reason = _quick_infeasible(d, trial)
        if reason is None:
            status, _ = _solve(d, trial, time_limit=MUS_TIME_LIMIT)
            infeasible = status == "infeasible"
            if status == "unknown":
                note = "incomplete"  # 无法证明该约束必要，保守跳过
        else:
            infeasible = True  # 快速判定已证明移除后仍无解
        if infeasible:
            active = trial  # 该约束与冲突无关，永久删除
        else:
            kept.append(cid)  # 删去它即可行 -> 属于最小冲突集

    return [d["specs"][cid] for cid in kept], note


def _multi_minimal_conflicts(
    d: Dict[str, Any],
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """多卷版本的 deletion-based MUS，单卷约束与跨卷上限统一参与删减。"""
    active = set(range(len(d["specs"])))

    note = "ok"
    reason = _quick_infeasible_multi(d, active)
    if reason is None:
        status, _ = _solve_multi(d, active, time_limit=MUS_TIME_LIMIT)
        if status == "unknown":
            return None, "求解器无法在限定时间内判定约束是否无解，未能给出冲突集"
        if status != "infeasible":
            return [], "ok"

    kept: List[int] = []
    for cid in range(len(d["specs"])):
        if cid not in active:
            continue
        trial = active - {cid}
        reason = _quick_infeasible_multi(d, trial)
        if reason is None:
            status, _ = _solve_multi(d, trial, time_limit=MUS_TIME_LIMIT)
            infeasible = status == "infeasible"
            if status == "unknown":
                note = "incomplete"
        else:
            infeasible = True
        if infeasible:
            active = trial
        else:
            kept.append(cid)

    return [d["specs"][cid] for cid in kept], note


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def solve_exam(req: Dict[str, Any]) -> Dict[str, Any]:
    d = _prepare(req)

    if d["P"] == 1:
        return _solve_single(d)
    return _solve_multi_papers(d)


def _solve_single(d: Dict[str, Any]) -> Dict[str, Any]:
    d["specs"] = _build_constraint_specs(d)
    active = set(range(len(d["specs"])))

    # 快速无解预判
    reason = _quick_infeasible(d, active)
    if reason is not None:
        conflicts, note = _minimal_conflicts(d)
        return _infeasible_result(conflicts, note, reason)

    status, chosen, note = _optimize(d)
    if status == "feasible":
        return _feasible_result(d, chosen)
    if status == "unknown":
        return {"status": "error", "error": note or "求解超时，无法判定可行性"}

    # 无解：逐项删减验证最小冲突集
    conflicts, note = _minimal_conflicts(d)
    return _infeasible_result(conflicts, note)


def _solve_multi_papers(d: Dict[str, Any]) -> Dict[str, Any]:
    d["specs"] = _build_multi_specs(d)
    active = set(range(len(d["specs"])))

    reason = _quick_infeasible_multi(d, active)
    if reason is not None:
        conflicts, note = _multi_minimal_conflicts(d)
        return _multi_infeasible_result(conflicts, note, reason)

    status, chosen, note = _multi_optimize(d)
    if status == "feasible":
        return _multi_feasible_result(d, chosen)
    if status == "unknown":
        return {"status": "error", "error": note or "求解超时，无法判定可行性"}

    conflicts, note = _multi_minimal_conflicts(d)
    return _multi_infeasible_result(conflicts, note)


def _infeasible_result(
    conflicts: Optional[List[Dict[str, Any]]],
    note: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    if conflicts is None:
        return {
            "status": "error",
            "error": note or "无法在限定时间内验证冲突约束集",
        }
    result: Dict[str, Any] = {
        "status": "infeasible",
        "message": reason or "不存在满足全部约束的试卷",
        "conflict_constraints": [
            {"kind": c["kind"], "detail": c["detail"]} for c in conflicts
        ],
    }
    if note == "incomplete":
        result["message"] += "（部分约束判定超时，冲突集可能不完整）"
    return result


def _multi_infeasible_result(
    conflicts: Optional[List[Dict[str, Any]]],
    note: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    if conflicts is None:
        return {
            "status": "error",
            "error": note or "无法在限定时间内验证冲突约束集",
        }
    result: Dict[str, Any] = {
        "status": "infeasible",
        "message": reason or "不存在满足全部约束的平行卷",
        "conflict_constraints": [_conflict_entry(c) for c in conflicts],
    }
    if note == "incomplete":
        result["message"] += "（部分约束判定超时，冲突集可能不完整）"
    return result


def _conflict_entry(c: Dict[str, Any]) -> Dict[str, Any]:
    entry = {"kind": c["kind"], "scope": c["scope"], "detail": c["detail"]}
    if c["scope"] == "paper":
        entry["paper"] = c["paper"]
    return entry


# --------------------------------------------------------------------------- #
# 可行结果整理
# --------------------------------------------------------------------------- #
def _paper_stats(d: Dict[str, Any], chosen: List[bool]) -> Dict[str, Any]:
    questions = d["questions"]
    selected = [questions[i] for i, ok in enumerate(chosen) if ok]
    selected_ids = [q["id"] for q in selected]

    total = sum(int(q["score"]) for q in selected)
    sd = sum(int(round(q["difficulty"] * SCALE)) * int(q["score"]) for q in selected)
    avg = sd / (SCALE * total) if total else 0.0
    deviation = abs(avg - d["target"])

    type_counts: Dict[str, int] = {}
    for q in selected:
        type_counts[q["type"]] = type_counts.get(q["type"], 0) + 1

    kp_counts: Dict[str, int] = {}
    for q in selected:
        for kp in q["knowledge_points"]:
            kp_counts[kp] = kp_counts.get(kp, 0) + 1
    repeated = sum(1 for c in kp_counts.values() if c >= 2)

    return {
        "paper": selected,
        "selected_ids": selected_ids,
        "total_score": total,
        "average_difficulty": round(avg, 6),
        "difficulty_deviation": round(deviation, 6),
        "type_counts": type_counts,
        "knowledge_point_counts": kp_counts,
        "repeated_knowledge_points": repeated,
    }


def _feasible_result(d: Dict[str, Any], chosen: List[bool]) -> Dict[str, Any]:
    return {
        "status": "feasible",
        **_paper_stats(d, chosen),
        "seed": d["seed"],
    }


def _multi_feasible_result(
    d: Dict[str, Any], chosen: List[List[bool]]
) -> Dict[str, Any]:
    P = d["P"]
    papers: List[Dict[str, Any]] = []
    selections: List[List[Any]] = []
    raw_dev_sum = 0.0  # 直接由放大整数计算，与求解器阶段 1 目标同源
    for p in range(P):
        stats = _paper_stats(d, chosen[p])
        questions = d["questions"]
        sd = sum(
            int(round(questions[i]["difficulty"] * SCALE)) * int(questions[i]["score"])
            for i, ok in enumerate(chosen[p]) if ok
        )
        raw_dev_sum += abs(sd - int(round(d["target"] * SCALE * d["T"]))) / (SCALE * d["T"])
        selections.append(stats["selected_ids"])
        papers.append({"paper_index": p, **stats})

    # 跨卷重复题：逐对统计共同题，仅记录确有共享的卷对
    shared_questions: List[Dict[str, Any]] = []
    total_shared = 0
    for a in range(P):
        set_a = set(selections[a])
        for b in range(a + 1, P):
            common = sorted(set_a & set(selections[b]), key=_id_sort_key)
            if not common:
                continue
            total_shared += len(common)
            shared_questions.append({
                "papers": [a, b],
                "shared_count": len(common),
                "shared_ids": common,
            })

    return {
        "status": "feasible",
        "paper_count": P,
        "max_shared_questions": d["max_shared"],
        "papers": papers,
        "total_difficulty_deviation": round(raw_dev_sum, 6),
        "total_shared_questions": total_shared,
        "shared_questions": shared_questions,
        "seed": d["seed"],
    }
