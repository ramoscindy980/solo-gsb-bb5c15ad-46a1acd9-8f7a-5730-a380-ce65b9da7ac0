"""Exam-paper composition solver built on OR-Tools CP-SAT.

择优规则（字典序，逐层最优后固定再优化下一层）：
  1. 难度偏差 |平均难度 - 目标难度| 最小；
  2. 重复知识点数（命中 >=2 道题的知识点个数）最少；
  3. 题目编号序列最小（按编号规范化排序后的 0/1 选择向量，分块编码）。

无解时通过“逐项删减验证”求删除极小的冲突约束集（deletion-based MUS）：
逐条尝试移除约束，若移除后仍无解则永久删除，最终保留的即为最小冲突集。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

SCALE = 1000  # 难度浮点 -> 整数的放大倍数
CHUNK_BITS = 20  # 编号序列分块编码的块大小，2^20 远在 int64 安全范围内
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
    }


# --------------------------------------------------------------------------- #
# 约束清单
# --------------------------------------------------------------------------- #
def _build_constraint_specs(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    """将用户请求展开为带编号、保留原始顺序的约束项。"""
    specs: List[Dict[str, Any]] = [
        {"kind": "total_score", "detail": {"total_score": d["T"]}}
    ]
    for t, c in d["type_counts"].items():
        specs.append({"kind": "type", "detail": {"type": t, "count": int(c)}})
    for kp, c in d["coverage"].items():
        specs.append(
            {"kind": "coverage", "detail": {"knowledge_point": kp, "min_count": int(c)}}
        )
    specs.append(
        {"kind": "difficulty_range", "detail": {"range": [d["diff_lo"], d["diff_hi"]]}}
    )
    for g in d["mutex_groups"]:
        specs.append({"kind": "mutex", "detail": {"group": list(g)}})
    return specs


# --------------------------------------------------------------------------- #
# 建模
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
    """在给定约束子集上求解，返回 (状态, 选择向量)。状态为 feasible/infeasible/unknown。"""
    m, x, _ = _build_model(d, active)
    s = _solver(d)
    s.parameters.max_time_in_seconds = time_limit
    status = s.Solve(m)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return "feasible", [bool(s.Value(x[i])) for i in range(len(x))]
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


# --------------------------------------------------------------------------- #
# 分层择优
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


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def solve_exam(req: Dict[str, Any]) -> Dict[str, Any]:
    d = _prepare(req)
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


def _feasible_result(d: Dict[str, Any], chosen: List[bool]) -> Dict[str, Any]:
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
        "status": "feasible",
        "paper": selected,
        "selected_ids": selected_ids,
        "total_score": total,
        "average_difficulty": round(avg, 6),
        "difficulty_deviation": round(deviation, 6),
        "type_counts": type_counts,
        "knowledge_point_counts": kp_counts,
        "repeated_knowledge_points": repeated,
        "seed": d["seed"],
    }
