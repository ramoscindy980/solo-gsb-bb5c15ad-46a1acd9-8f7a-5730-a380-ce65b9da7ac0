# 智能组卷 API

面向教研组的约束组卷服务：提交**题库快照**与**总分、题型数量、知识点覆盖、难度区间、互斥题组**等约束，自动生成分值精确、约束满足、结果可复现的试卷；支持一次请求生成 **2～5 份平行卷**（同一个联合 CP-SAT 模型，各卷分别满足全部单卷约束，并保证任意两卷共同题数不超过上限）。

多组可行解按确定性规则依次择优：**难度偏差 →（多卷模式为跨卷重复题总数；单卷模式为重复知识点数）→ 题目编号序列**；无解时返回经逐项删减验证的**最小冲突约束集**，多卷模式下冲突项区分**单卷约束**与**跨卷重叠上限**。

技术栈：Python · FastAPI · SQLite · OR-Tools CP-SAT · Docker / Compose。

## 启动

```bash
docker compose up --build
```

容器启动时自动安装依赖并创建 SQLite 数据库，无需任何手动初始化。

- API 地址：http://localhost:8000
- 端口：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 的 `ports` 修改）
- 必要配置（环境变量，均有默认值，一般无需修改）：
  - `EXAM_DB_PATH`：SQLite 数据库文件路径，默认 `/data/exam.db`（已挂载为持久卷 `exam_data`）
- 交互式文档（Swagger）：http://localhost:8000/docs

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` `/api/tasks` | 创建组卷任务（异步），返回 `task_id` |
| `GET`  | `/api/tasks/{task_id}` | 查询任务状态与结果 |
| `GET`  `/health` | 健康检查 |

任务状态：`pending` → `running` → `done`；结果在 `result` 字段中返回（`feasible` / `infeasible` / `error`）。

## 请求字段

- `questions`：题库快照，提交后冻结。每题含 `id`（数字或字符串，唯一）、`score`（正整数）、`type`、`difficulty`（0~1）、`knowledge_points`。
- `total_score`：试卷总分，**精确等于**。
- `type_counts`：各题型的精确数量。
- `knowledge_coverage`：知识点 → 试卷中至少出现的题目数。
- `difficulty_range`：试卷平均难度（按分值加权）闭区间 `[min, max]`。
- `target_difficulty`：期望平均难度，缺省取难度区间中点；难度偏差为 `|平均难度 - 目标难度|`。
- `mutex_groups`：互斥题组，每组至多选取 1 题。
- `paper_count`：平行卷数量，取值 **2～5**；缺省（或为 1）时按**单卷模式**响应。
- `max_shared_questions`：任意两卷共同题数上限，非负整数；多卷模式缺省为 `0`（平行卷互不重题）。单卷模式不可设置。
- `seed`：随机种子（默认 42），相同请求 + 相同种子结果可复现。

## 请求示例（单卷）

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考",
    "questions": [
      {"id": 1, "score": 5,  "type": "single_choice", "difficulty": 0.2, "knowledge_points": ["集合"]},
      {"id": 2, "score": 5,  "type": "single_choice", "difficulty": 0.5, "knowledge_points": ["函数"]},
      {"id": 3, "score": 5,  "type": "single_choice", "difficulty": 0.8, "knowledge_points": ["导数"]},
      {"id": 4, "score": 12, "type": "essay",         "difficulty": 0.6, "knowledge_points": ["函数", "导数"]},
      {"id": 5, "score": 13, "type": "essay",         "difficulty": 0.7, "knowledge_points": ["数列"]}
    ],
    "total_score": 20,
    "type_counts": {"single_choice": 2, "essay": 1},
    "knowledge_coverage": {"函数": 1},
    "difficulty_range": [0.3, 0.7],
    "target_difficulty": 0.5,
    "mutex_groups": [[2, 4]],
    "seed": 42
  }'
```

## 请求示例（多份平行卷）

一次生成 3 份卷，每份总分 30、题型/知识点/难度约束完全一致，任意两卷**不得共用题目**
（`max_shared_questions: 0`，多卷模式下的缺省值；如需允许重题可放宽，例如 2）：

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考·A/B/C 卷",
    "questions": [
      {"id": 1, "score": 5,  "type": "single_choice", "difficulty": 0.40, "knowledge_points": ["集合"]},
      {"id": 2, "score": 5,  "type": "single_choice", "difficulty": 0.50, "knowledge_points": ["函数"]},
      {"id": 3, "score": 5,  "type": "single_choice", "difficulty": 0.60, "knowledge_points": ["函数"]},
      {"id": 4, "score": 5,  "type": "single_choice", "difficulty": 0.45, "knowledge_points": ["导数"]},
      {"id": 5, "score": 5,  "type": "single_choice", "difficulty": 0.55, "knowledge_points": ["数列"]},
      {"id": 6, "score": 5,  "type": "single_choice", "difficulty": 0.50, "knowledge_points": ["集合"]},
      {"id": 7, "score": 5,  "type": "single_choice", "difficulty": 0.52, "knowledge_points": ["函数"]},
      {"id": 8, "score": 5,  "type": "single_choice", "difficulty": 0.48, "knowledge_points": ["导数"]},
      {"id": 9,  "score": 10, "type": "essay", "difficulty": 0.40, "knowledge_points": ["函数"]},
      {"id": 10, "score": 10, "type": "essay", "difficulty": 0.50, "knowledge_points": ["导数"]},
      {"id": 11, "score": 10, "type": "essay", "difficulty": 0.55, "knowledge_points": ["数列"]},
      {"id": 12, "score": 10, "type": "essay", "difficulty": 0.45, "knowledge_points": ["函数", "导数"]},
      {"id": 13, "score": 10, "type": "essay", "difficulty": 0.52, "knowledge_points": ["函数"]},
      {"id": 14, "score": 10, "type": "essay", "difficulty": 0.48, "knowledge_points": ["导数"]},
      {"id": 15, "score": 10, "type": "essay", "difficulty": 0.50, "knowledge_points": ["数列"]},
      {"id": 16, "score": 10, "type": "essay", "difficulty": 0.46, "knowledge_points": ["函数"]}
    ],
    "total_score": 30,
    "type_counts": {"single_choice": 2, "essay": 2},
    "knowledge_coverage": {"函数": 1},
    "difficulty_range": [0.35, 0.65],
    "target_difficulty": 0.5,
    "mutex_groups": [[1, 6], [2, 7]],
    "paper_count": 3,
    "max_shared_questions": 0,
    "seed": 42
  }'
```

## 可行结果（单卷）

```json
{
  "status": "feasible",
  "selected_ids": [1, 2, 4],
  "total_score": 20,
  "average_difficulty": 0.48,
  "difficulty_deviation": 0.02,
  "type_counts": {"single_choice": 2, "essay": 1},
  "knowledge_point_counts": {"集合": 1, "函数": 2, "导数": 1},
  "repeated_knowledge_points": 1,
  "paper": [ /* 完整题目对象 */ ]
}
```

## 可行结果（多卷）

结果在 `papers` 数组中按卷序号返回，顶层汇总三项择优指标；各卷字段与单卷结果一致。

```json
{
  "status": "feasible",
  "paper_count": 3,
  "max_shared_questions": 0,
  "papers": [
    {
      "paper_index": 0,
      "selected_ids": [7, 8, 10, 15],
      "total_score": 30,
      "average_difficulty": 0.5,
      "difficulty_deviation": 0.0,
      "type_counts": {"single_choice": 2, "essay": 2},
      "knowledge_point_counts": {"函数": 1, "导数": 2, "数列": 1},
      "repeated_knowledge_points": 1,
      "paper": [ /* 完整题目对象 */ ]
    },
    {
      "paper_index": 1,
      "selected_ids": [4, 5, 13, 14],
      "total_score": 30,
      "average_difficulty": 0.5,
      "difficulty_deviation": 0.0,
      "type_counts": {"single_choice": 2, "essay": 2},
      "knowledge_point_counts": {"函数": 1, "导数": 2, "数列": 1},
      "repeated_knowledge_points": 1,
      "paper": [ /* 完整题目对象 */ ]
    },
    {
      "paper_index": 2,
      "selected_ids": [2, 3, 9, 11],
      "total_score": 30,
      "average_difficulty": 0.5,
      "difficulty_deviation": 0.0,
      "type_counts": {"single_choice": 2, "essay": 2},
      "knowledge_point_counts": {"函数": 3, "数列": 1},
      "repeated_knowledge_points": 1,
      "paper": [ /* 完整题目对象 */ ]
    }
  ],
  "total_difficulty_deviation": 0.0,
  "total_shared_questions": 0,
  "shared_questions": []
}
```

汇总字段：

- `total_difficulty_deviation`：各卷难度偏差之和（首要择优指标）。
- `total_shared_questions`：跨卷重复题总数 `Σ_{a<b} |卷a ∩ 卷b|`（次要择优指标，受 `max_shared_questions` 上限约束）。
- `shared_questions`：逐对卷统计的共同题明细，形如
  `{"papers": [0, 1], "shared_count": 2, "shared_ids": [7, 12]}`；无共同题的卷对不列出。

**多卷择优规则**（全部试卷在同一个联合 CP-SAT 模型中求解，确定性且可复现）：

1. 各卷难度偏差**总和**最小；
2. 跨卷重复题总数最少；
3. 各卷题号序列最小（按卷序号拼接、题目按编号规范化排序后的 0/1 选择向量字典序最小）。

未传 `paper_count` 时响应结构与以往完全一致（单卷字段在顶层，不返回 `papers`）。

## 无解结果（最小冲突约束集）

当约束无解时，`status` 为 `infeasible`，`conflict_constraints` 给出经**逐项删减验证**的删除极小冲突集——即其中任何一条被放宽/删除后，其余约束即可满足。

单卷模式：

```json
{
  "status": "infeasible",
  "message": "不存在满足全部约束的试卷",
  "conflict_constraints": [
    {"kind": "total_score",      "detail": {"total_score": 20}},
    {"kind": "difficulty_range", "detail": {"range": [0.9, 1.0]}}
  ]
}
```

多卷模式下每条冲突额外带 `scope` 与（仅单卷约束）`paper`，以区分**单卷约束**与**跨卷重叠上限**：

```json
{
  "status": "infeasible",
  "message": "题库总分不足，无法在不共享题目的条件下组成 2 份总分 20 的平行卷",
  "conflict_constraints": [
    {"kind": "type",         "scope": "paper", "paper": 0,
     "detail": {"type": "single_choice", "count": 4}},
    {"kind": "type",         "scope": "paper", "paper": 1,
     "detail": {"type": "single_choice", "count": 4}},
    {"kind": "shared_limit", "scope": "cross",
     "detail": {"papers": [0, 1], "max_shared_questions": 0}}
  ]
}
```

- `scope: "paper"`：某一份卷（`paper` 为从 0 开始的卷序号）自身的约束冲突，
  `kind` 取值 `total_score` / `type` / `coverage` / `difficulty_range` / `mutex`。
- `scope: "cross"`：跨卷重叠上限，`kind` 固定为 `shared_limit`，
  `detail.papers` 给出卷对序号。
- 若无解完全由单卷自身约束导致（例如某卷的难度区间与题型数量冲突），冲突集中只含
  `scope: "paper"` 的项；仅当跨卷上限参与冲突时才会出现 `scope: "cross"`。
