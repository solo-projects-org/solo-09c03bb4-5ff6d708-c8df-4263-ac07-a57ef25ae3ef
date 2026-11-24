# 胶片扫描取帧清单服务

一卷胶片被扫描了多次，档案员登记每份扫描的文件名、档内帧号偏移与原片帧号
可用段，系统按帧段编排一张**换文件次数最少**的取帧清单，供修复组从不同文件
里取齐原片。

- Web 框架：FastAPI（SQLite 持久化，Swagger 在线试单：`/docs`）
- 编排：纯区间算法（切点 + Dijkstra 最短路），**不逐帧展开**，10 万帧规模亚毫秒级
- 事务：重编成功才一起更新扫描记录与清单；失败整体回滚，旧记录和旧清单继续生效

## 领域规则

1. 一卷胶片登记原片帧号范围 `[first_frame, last_frame]`。
2. 每份扫描登记：文件名（卷内唯一）、偏移 `offset`、若干原片帧号可用段；
   **档内帧号 = 原片帧号 − offset**。
3. 清单必须无缝覆盖整卷；每段列明来源文件与档内起止帧。
4. 换文件时，**接入下一份文件前的两帧**必须在新旧文件中都可用（调色对照帧），
   清单的 `switches` 中给出两帧在两份文件里的档内帧号。
5. 目标是换文件次数最少；同代价按文件名序列字典序裁决，结果确定。
6. 已修复的同源帧段可**锁定**：重编时该区间必须保留指定文件与档内帧号，且
   不会被切点拆开。
7. 排不出时返回 409，并报出**第一个接不上的原片帧** `first_gap_frame`；
   本次提交整体回滚，原记录与清单继续生效。

## 启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

容器内 SQLite 位于 `/data/film_archive.db`，由命名卷 `film-data` 持久化。
设置 `SEED_SAMPLE=1`（compose 默认开启）时，启动会幂等写入一卷示例
`sample-reel`（原片 1..240，两份扫描在 81..120 重叠），可直接在 Swagger 试单。

本地无 Docker 时的等价启动：

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
FILM_DB_PATH=./film_archive.db SEED_SAMPLE=1 \
  .venv/bin/uvicorn app.main:app --reload --port 8000
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rolls` | 登记胶片卷，可同时录入扫描并立即编排（失败不写入） |
| GET | `/rolls` | 列出所有卷 |
| GET | `/rolls/{id}` | 卷详情：扫描、可用段、锁定段 |
| PUT | `/rolls/{id}/scans` | 补交扫描 / 更正可用段（整体替换），成功才重编落库 |
| POST | `/rolls/{id}/locks` | 锁定已修复帧段并重编 |
| DELETE | `/rolls/{id}/locks/{lock_id}` | 解锁并重编 |
| POST | `/rolls/{id}/replan` | 按当前记录重新编排 |
| GET | `/rolls/{id}/manifest` | 查询当前取帧清单 |

### 建档请求示例

```json
{
  "name": "reel-A",
  "first_frame": 1,
  "last_frame": 240,
  "scans": [
    {"filename": "scan01.dpx", "offset": 0,
     "segments": [{"start_frame": 1, "end_frame": 120}]},
    {"filename": "scan02.dpx", "offset": -80,
     "segments": [{"start_frame": 81, "end_frame": 240}]}
  ]
}
```

### 清单响应示例（节选）

```json
{
  "switch_count": 1,
  "runs": [
    {"filename": "scan01.dpx", "orig_start": 1, "orig_end": 120,
     "file_start": 1, "file_end": 120, "locked": false},
    {"filename": "scan02.dpx", "orig_start": 121, "orig_end": 240,
     "file_start": 201, "file_end": 320, "locked": false}
  ],
  "switches": [
    {"at_frame": 121, "from_filename": "scan01.dpx", "to_filename": "scan02.dpx",
     "overlap_frames": [119, 120],
     "from_file_frames": [119, 120],
     "to_file_frames": [199, 200]}
  ]
}
```

### 排不出时的响应

```json
{"detail": {"error": "planning_failed",
            "message": "无法从原片帧 121 起继续编排……",
            "first_gap_frame": 121}}
```

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

- `tests/test_planner.py`：编排算法（两帧对照、最少切换、锁定保留、首断帧、
  段合并、非 1 起始帧号、确定性裁决等）
- `tests/test_api.py`：HTTP 全流程（建档、失败零写入、失败回滚旧清单仍生效、
  补交扫描、锁定/解锁、Swagger 可达）

## 目录结构

```
app/
  planner.py   # 纯函数编排器（区间/Dijkstra，无框架依赖）
  models.py    # SQLAlchemy 模型
  schemas.py   # Pydantic 模式与 Swagger 示例
  main.py      # FastAPI 接口与事务边界
tests/         # pytest 测试
Dockerfile, docker-compose.yml, requirements.txt
```
