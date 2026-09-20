# 湾区代表队行程联控

协调粤港澳体育代表队在正式比赛、校园交流、企业参访、主题市集与接驳车队之间的行程。
核心原则：

- **正式比赛是不可移动的外部约束**。系统只从队伍"赛后可用窗口"中扣除比赛占用；
  加时只延长比赛结束时间，任何环节都不会被系统排进比赛时段，比赛本身也不接受改期。
- **参与方确认后才锁定资源**。候选行程不占容量/座位；锁定瞬间复核容量、申报窗口与
  最晚确认点，缺少任何必要方确认（办公室、队伍、接待方/车队）都会被拒绝。
- **只重排尚未出发的环节**。环节状态进入 `enroute`（在途）/`in_progress`（签到中）/
  `completed` 后即冻结，加时或车辆故障只会改排时间链上其后的未出发后缀，
  在途与已签到人员不会被强行改派。
- **按用途授权、最小可见**。接待方只能看到完成本次服务必需的信息：
  包餐接待方仅在成员就 `meal_service` 用途授权后看到饮食禁忌；
  申报了公开传播的接待方仅看到 `publicity` 影像授权状态；车队只看核载与无障碍登车信息。
  凭证标识不出现在接待方名单里。
- **损失只记录、不扣款**。取消产生的物料损失与车辆空驶费生成待双方确认的记录
  （`settlement: external_record_only`），双方确认后仅作留痕。
- **签到按凭证幂等去重**。重复扫描同一凭证不会重复计数，汇总人数按唯一凭证统计。

## 目录

- `service.py` — 运行入口（`--check` 自检、`--seed` 装载样例）
- `app/service.py` — 领域核心：申报、候选、确认锁定、扰动改排、签到、授权、损失
- `app/store.py` — 线程安全内存存储与资源占用台账
- `app/api.py` — 标准库 HTTP/JSON 路由
- `app/bootstrap.py` — fixtures 装载
- `fixtures/sample.json` — 两天全明星赛、四支队伍、三类接待方与车队的申报样例

## 运行

```bash
python3 service.py --check            # 基础自检（含种子装载）
python3 service.py --seed --port 8000 # 启动并预载样例数据
python3 -m unittest discover -v       # 32 项契约/链路测试
```

## 环节状态机

```
proposed ──全部必要方确认+lock成功──▶ locked ──depart──▶ enroute
   │                                  │ │                   │
   │                              cancel  checkin      in_progress
   ▼                                  ▼ ▼                   │
superseded / canceled            canceled ◀──── complete ───┘
```

`proposed` 改排后旧环节置 `superseded` 并保留 `superseded_by` 链；冻结环节永不进入该链。

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/admin/seed` | 装载（或重置为）样例数据 |
| POST | `/teams` `/hosts` `/competitions` | 队伍 / 接待方 / 正式比赛申报 |
| POST | `/teams/{id}/members` | 成员报名：别名、凭证、饮食、无障碍、按用途授权 |
| POST | `/teams/{id}/windows` | 队伍赛后可用窗口（比赛占用自动扣除） |
| POST | `/hosts/{id}/offerings` | 学校/企业/市集申报：时段、容量、语言、无障碍、包餐、影像用途、最晚确认点 |
| POST | `/hosts/{id}/fleets` | 车队申报：车型座位、无障碍、运营区域、最晚确认点、空驶费 |
| POST | `/teams/{id}/candidates` | 生成候选行程（不占资源） |
| POST | `/plans/{id}/confirmations` | 参与方确认（可按环节，可批量确认本方环节） |
| POST | `/plans/{id}/lock` | 确认齐备后锁定资源（锁定瞬间复核） |
| POST | `/items/{id}/depart` | 标记出发（环节冻结） |
| POST | `/items/{id}/checkins` | 凭证签到，重复扫描返回 `duplicate: true` |
| GET | `/checkins/summary` | 去重汇总：扫描数 vs 唯一人数 |
| POST | `/incidents` | 录入 `overtime` / `vehicle_breakdown` / `traffic_delay`，立即返回影响面 |
| GET | `/incidents/{id}` | 受影响对象、冻结/可改排环节、可选改排方案 |
| POST | `/plans/{id}/replan` | 选定方案：仅替换未出发后缀，冻结环节不动，新环节需重新确认 |
| POST | `/plans/{id}/cancel` | 取消未出发环节，在途/签到环节保留 |
| GET | `/items/{id}/manifest?viewer=` | 接待方最小可见名单（按用途授权） |
| GET/POST | `/losses`、`/losses/{id}/confirmations` | 损失记录与双方确认（不扣款） |
| GET/POST | `/notifications`、`/notifications/{id}/ack` | 通知与回执 |
| GET | `/office/overview` | 交流办公室一屏视图：待确认、扰动、回执、去重人数、损失记录 |

### 扰动响应示例

`POST /incidents` 录入加时赛后，响应直接给出办公室需要的四件事：

1. `affected_parties` / `plans[].pinned_items` — 受影响对象与已冻结、不可强改的环节；
2. `plans[].reschedulable_items` — 可改排的未出发后缀及原因（`competition_overtime`、
   `chain_shift`、`vehicle_unavailable` 等）；
3. `plans[].options` — 候选改排方案（抛锚车队自动退出备选，改排环节以出发前为最晚确认点）；
4. `notifications` — 各方通知的送达/回执状态，可用 `/notifications/{id}/ack` 补回执。

## 设计约束

- 纯标准库实现（`http.server`），无外部运行时依赖；线程安全的内存存储便于直接联调，
  持久化可通过替换 `Store` 落地。
- 时间统一为带时区的 ISO-8601（裸时间按 UTC+8 处理），排期按 15 分钟网格枚举。
- 容量按时间窗并发占用核算；候选阶段以临时占用副本模拟同行程内不重复订座。
