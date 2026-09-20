# 湾区代表队行程联控

协调粤港澳体育代表队在**正式比赛、校园交流课、智造企业参访、主题市集、接驳车队**
之间的行程后端。比赛是不可移动的外部约束；只有参与方确认后的活动才锁定资源；
加时或车辆故障发生时，系统只重排尚未出发的环节，已在途或已签到人员不被强行改派。

## 业务规则

### 申报与候选

* 学校、企业、市集、车队分别申报可用时段、容量、翻译语言、无障碍条件与
  **最晚确认点**（`confirm_by`）；车队另报路线与车牌。
* 队伍申报可用窗口；系统由赛程自动派生**赛后窗口**（实际结束 + 15 分钟恢复
  缓冲，至当日 21:00）。
* `POST /teams/{id}/candidates` 结合赛后窗口、比赛硬约束、容量、翻译、无障碍、
  车队路线给出每条需求的候选与**被拒原因**；候选不预占资源。
* 已过最晚确认点不消灭候选，而以 `confirm_by_passed: true` 标记——加时后的
  改约仍可促成，但办公室必须再次取得接待方确认。

### 确认与锁定

* 候选经双方（队伍 + 接待方）逐环节确认后，才锁定时段容量/车辆座位。
* 容量实时校验：锁定时超容返回 `capacity_exhausted`，候选中直接显示剩余容量。
* 环节生命周期：`proposed → confirmed → departed → in_progress → completed`；
  取消为 `cancelled`，扰动改排为 `rescheduled`（并指向替代环节）。

### 扰动与改排

* `POST /disruptions/overtime`（加时，更新比赛实际结束时间）与
  `POST /disruptions/vehicle-fault`（车辆故障，暂停运力）立即生成影响面：
  受影响环节、受保护人数（在途按发车名单、到场按签到凭证去重）、
  可选改排、各方确认状态、通知与回执。
* `POST /disruptions/{id}/reschedule` 只替换**未出发**环节；
  `departed/in_progress/completed` 一律跳过并给出受保护人数。
* 替代环节为新的 `proposed` 环节，释放旧容量、**必须双方重新确认**才重新锁定。

### 签到与汇总

* 现场只用参与者**别名与凭证标识（badge）**，系统不保存证件图像。
* 同一凭证在同一环节重复扫码只计一次（`duplicates` 返回重复记录，
  `checkin_scans` 保留扫码总次数）。
* `GET /attendance` 按凭证跨环节去重汇总：`total_unique_persons`
  排除重复签到。

### 授权与最小可见

* 团体饮食（`scope=meal`，用途 `catering`）与影像公开意愿
  （`scope=image`，用途 `publicity`）由队伍按用途、按接待方授权；
  撤销立即生效。
* 接待方只看到完成服务必需的信息：场地接待方可见时间、人数、语言/无障碍需求、
  签到别名与凭证；**车队**只见时间、人数、路线与无障碍乘车汇总计数，
  不接触个人标识、饮食与影像。
* 身份通过 `X-Viewer: office | team:<id> | party:<id>` 头传递；
  非接待方访问他方行程返回 403。

### 取消与损失

* 未出发环节可取消并释放容量；已出发/已签到环节取消返回 `leg_immutable`。
* 物料损失（`material`）与车辆空驶（`vehicle_empty`）只生成记录，
  须**办公室与接待方双方确认**（`pending_confirmation → confirmed_record`），
  系统从不自动扣款（`settled` 恒为 `false`）。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/bootstrap` | 初始化赛事 |
| POST | `/teams`、`/teams/{id}/roster`、`/teams/{id}/availability` | 队伍、名单、可用窗口 |
| POST | `/games` | 登记正式比赛（不可移动） |
| POST | `/parties`、`/offers` | 接待方与可用时段申报 |
| POST | `/offers/{id}/resume` | 车辆修复后恢复运力 |
| POST | `/teams/{id}/candidates` | 生成候选行程与拒绝原因 |
| POST | `/teams/{id}/itineraries` | 按候选选择建单 |
| GET | `/itineraries`、`/itineraries/{id}` | 行程（按观看者裁剪） |
| POST | `/legs/{id}/confirm`、`/depart`、`/checkin`、`/complete`、`/cancel` | 环节生命周期 |
| GET | `/attendance?team_id=&leg_id=` | 去重签到汇总 |
| POST | `/consents`、`/consents/{id}/revoke` | 用途授权与撤销 |
| POST | `/losses`、`/losses/{id}/confirm` | 损失登记与双方确认 |
| POST | `/disruptions/overtime`、`/disruptions/vehicle-fault` | 扰动进入 |
| GET | `/disruptions/{id}/impact` | 影响面（对象/候选/确认/回执） |
| POST | `/disruptions/{id}/reschedule`、`/close` | 改排与关闭 |
| GET | `/notifications[/{party}]`、`POST /notifications/{id}/ack` | 通知与回执 |

所有时间为带偏移 ISO8601（如 `2026-09-12T13:45:00+08:00`）。
可用 `X-Now` 头注入当前时间以便复盘与测试。

## 运行

```bash
python3 service.py --check            # 自检服务身份与种子数据
python3 service.py --port 8000        # 启动并载入 fixtures/sample.json
python3 -m unittest discover -s tests # 25 项契约/端到端测试
```

`fixtures/sample.json` 含两天四队、四场比赛、校园/企业/市集与两支接驳车队
的完整演练数据与三条用途授权。启动后 `GET /health` 查看健康状态。

## 代码结构

```
app/
  timeutil.py     时间窗、赛后窗口与不可占用区间
  store.py        线程安全内存状态库（可序列化）
  registry.py     队伍/比赛/接待方/申报/用途授权
  planning.py     候选生成与硬约束求解
  lifecycle.py    建单、确认锁定、出发/签到/完成、取消、损失双确
  disruptions.py  加时/故障影响面与改排（保护在途与已签到人员）
  notify.py       通知与回执
  views.py        按用途授权裁剪的最小可见视图
  server.py       JSON HTTP API
  seed.py         种子数据装载
```
