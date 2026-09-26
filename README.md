# 急救车调度与目的地分流

纯Python标准库实现的急救车调度与目的地分流原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 床位承诺（防止一张床答应两次）

确认派车（`assign`）时，系统会在**同一个数据库事务**内完成「回收到期占用 → 检查医院余床 → 写入床位承诺并占用一张床 → 任务状态流转」。并发派车通过 `BEGIN IMMEDIATE` 串行化，因此最后一张床只会被一辆车拿到，失败者收到 `409 bed_shortage`，任务状态不变、留在待派区。

承诺生命周期：

- `held`：已占用，带到期时间（默认30分钟，派车时可用 `hold_minutes` 指定，1–240 分钟）。
- 任务 `cancel`：占用立即退回，记录退回原因（如“任务取消：xxx”）。
- 迟迟未到场（超过到期时间仍未 `arrive`）：后台守护线程每30秒、服务重启恢复时、以及查询/派车事务内惰性回收，自动退回，原因记为“超时未到场，承诺自动退回”。
- 车辆 `arrive` 到场后到期时间清除，占用保持到交接；`handover` 交接后承诺转为 `admitted`（已收治），床位计入已收治数。
- 所有承诺与退回事件均持久化在 SQLite，服务重启后仍可查询；时间线中包含 `bed_released` 事件。

## 模块结构

- `app.py`：命令行参数、依赖组装、清扫线程启停和服务启动。
- `src/domain.py`：领域数据类型、错误（含 `BedShortage`）和基础校验。
- `src/rules.py`：状态转换、优先级评分、能力匹配、车辆冲突、承诺时长与医院校验。
- `src/repository.py`：SQLite建表、床位总账/承诺表、原子占床事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发、待派区缺床标注和审计。
- `src/sweeper.py`：到期承诺后台清扫线程（启动时先结算一次）。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：调度看板（医院总览、待派区、任务操作、床位承诺）。
- `tests/`：完整流程、规则计算、失败场景和床位承诺（含并发不超卖）测试。

## 数据表

- `records` / `audit_events`：任务记录与审计时间线。
- `hospitals`：医院床位总账（`total_beds`）。创建任务时若医院未登记，按申报床位数自动建账；也可用接口显式登记/调整。
- `bed_commitments`：每笔床位承诺，字段含 `status`（held/admitted/released）、`expires_at`、`arrived_at`、`released_at`、`release_reason`。同一任务至多存在一条 `held` 承诺（数据库部分唯一索引保证）。
- 余床 = `total_beds − held 占用数 − admitted 收治数`。

## 启动

```bash
python3 app.py --db ./data.db --port 8322
```

默认端口为`8322`，默认数据库位于项目目录。服务启动时自动建表并立即结算一次停机期间到期的占用。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：调度看板页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作（assign/enroute/arrive/transport/handover/cancel），请求体为`{"expected_version":1,"data":{"hold_minutes":30,...}}`。`assign` 无余床时返回 409，`details` 中包含 `total_beds/held_beds/admitted_beds/available_beds/shortage_beds`。
- `GET /api/pending`：待派区（received 任务），每项带余床与 `shortage_beds` 缺床数及 `dispatchable` 标记。
- `GET /api/hospitals`：医院床位总览（总床/占用中/已收治/余床）。
- `POST /api/hospitals`：登记或更新医院，请求体为`{"data":{"name":"...","total_beds":3,"capabilities":["ALS"],"note":"..."}}`。
- `GET /api/hospitals/{name}`：查看某医院的床位承诺（可带`status`过滤，名字需URL编码）。
- `GET /api/commitments`：全部承诺列表，可带`status`、`hospital`、`limit`；每项含到期时间和退回原因。
- `POST /api/commitments/expire`：立即结算到期占用（返回本次退回列表）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及床位承诺的并发不超卖、缺床留待派、取消退回、超时退回、到场豁免到期、交接收治与重启持久化。
