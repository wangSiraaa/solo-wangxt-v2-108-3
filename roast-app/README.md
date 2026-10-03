# 烘焙批次曲线对比系统

面向烘焙负责人的**过程记录**工具：并排查看豆温、环境温度与操作事件（回温点、一爆、风门变化、出锅），
而不是用成品评分替代过程。系统**不连接真实烘焙机**，数据来自带噪声、不均采样与探针失联的合成生成器，
或操作员在现场离线记录、带回工作台的**本地观察包**（文件只在浏览器本地读取，绝不上传）。

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | Svelte 4 + Vite + ECharts 5 |
| API | FastAPI（Pydantic 校验） |
| 计算 | NumPy：温升率、插值、阶段指标，全部为纯函数 |
| 存储 | PostgreSQL（原始采样、下豆点、人工标记），SQLAlchemy ORM |
| 测试 | pytest，同一套用例在 SQLite 与 PostgreSQL 上运行 |

## 数据与口径（重要）

### 温升率 RoR —— 窗口必须说明
采样间隔不均（1–5 s 抖动），因此不用相邻点差分。在每个**实测**时刻 t，取居中时间窗
`[t−W/2, t+W/2]`（默认 W=30 s）内的实测豆温点做普通最小二乘直线拟合，取斜率换算 °C/min。
- 至少 4 个实测点、时间跨度 ≥10 s 才给出 RoR，否则为 null（不编造）；
- **插值点不参与拟合**；探针失联的宽缺口处 RoR 直接断档；
- 序列边缘窗口被截断，返回值带 `ror_edge=true` 标记；
- 前端另有一个“显示平滑”参数（居中均值），只作用于展示曲线，窗口本身随接口参数和图表标题一起返回。

### 缺测与插值 —— 插值段不冒充实测
- `samples` 表**只存实测**：探针失联时豆温为 NULL，绝不回写；
- 查询时对 ≤`max_gap_fill_s`（默认 45 s）的内缺口做**相邻实测点线性插值**，
  逐点带 `is_interpolated=true`，图上为**虚线+空心菱形**，图例单列“插值段（非实测）”；
- 超过桥接上限的缺口与端点缺测**不填充**，曲线断档；缺测段在“缺测与插值审计”表逐条列出（通道、时长、处理方式）。

### 事件 —— 人工修正并保留来源
- 事件为只追加（append-only）。人工提交同类型事件时，旧行置 `superseded=true` 并记录
  `superseded_by_id`，不删除；自动建议记 `source=auto`，人工记 `source=manual`+`created_by`；
- 风门变化允许多条并存（离散操作点），金色虚线标出。

### 离线观察包 —— 本地文件、待核验、一次性应用（不上传）
现场记录可打包成 JSON **观察包**，带回工作台后从界面选择**本地文件**导入；浏览器仅本地读取并提交给
同源的本机 API，**不连接真实烘焙机、不发往任何外部服务**。

包格式 v1（自描述、可验真）：

| 字段 | 说明 |
|---|---|
| `format_version` | 格式版本，当前 `1`；其他版本整包拒绝 |
| `package_id` | 稳定包标识（同一标识即同一次投递，用于幂等与去重） |
| `generated_at` | 现场生成时间（ISO 8601） |
| `batch` | 批次信息：name/roaster/bean/charge_at/charge_temp_c/ambient_temp_c… |
| `samples[]` | 原始实测点：`t_s`（自下豆秒，可不均）、`bean_temp_c`/`env_temp_c`（缺测为 `null`） |
| `events[]` | 现场事件：稳定 `event_uid`、可选 `supersedes_uid`（包内修正链）、类型、`t_s`、来源 |
| `digest` | `{"algorithm":"sha256","sha256":…}`，对前述字段规范化 JSON 的摘要 |

摘要口径：对 `{format_version, package_id, generated_at, batch, samples, events}` 做
`json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)` 后取 `sha256`；
`digest` 块本身不参与签名。**摘要不一致即整包失败**。

导入生命周期（全部可审计）：

1. **接收 → `pending_review`**：只在 `import_batches` 账本落一份原文与摘要，**不写任何样本/事件**；
   同时对目标批次做冲突检测（结果存 `import_conflicts`）。
2. **预览**：用与应用完全相同的纯函数合并逻辑生成“目标曲线 + 指标 + 事件历史”投影，**不写库**。
3. **冲突裁决**：同一时刻已有不同实测值（`value_mismatch`）、或已存为空而包内补来读数
   （`missing_fill`）都必须逐条选择“保留已有 / 采用包内”。**未裁决前当前分析一点不变**。
4. **一次性应用**：单事务写入；任何错误整体回滚，**不存在半套曲线**。应用结果（计数、批次、裁决）
   存回账本作为幂等结果。

去重与顺序规则：
- **重复投递**：同 `package_id` 且摘要相同，返回原账本行/原应用结果，不新增样本或事件；同标识但内容
  不同返回 409。
- **同值重复**：同一时刻读数一致（含 1e-3 容差）直接跳过；缺测（null）绝不覆盖已有实测。
- **乱序到达**：后到的“更早片段”按 `t_s` 并入，来源仍记对应包。
- **事件**：继续遵守只追加 + supersede 历史；`(batch_id, event_uid)` 去重，包内 `supersedes_uid`
  必须指向同类型、不晚于自身的已存在事件。
- **整包失败**（`failed` 账本行，附全部发现项）：不支持的版本、摘要不符、非法/非有限时间、
  包内重复 `t_s`、重复 `event_uid`（重复事件链）、supersede 悬空或类型不符、supersede 指向更晚
  事件、阶段锚点逆序（如 charge 晚于 turning point）、风门缺开度/越界、解析失败等。数据库的
  批次/样本/事件表**没有任何残留变更**。
- 每个导入样本/事件都保留 `source`（`imported`/录制来源）与 `source_package_id`；曲线、事件表、
  导出 JSON、`/api/recompute` 独立重算均可逐点/逐条看到来源包。

### 发展时间比 —— 明确区间
| 指标 | 区间 |
|---|---|
| 脱水期 drying | 下豆 charge → 回温点 turning_point |
| 梅纳期 maillard | 回温点 → 一爆开始 first_crack_start |
| 发展期 development | 一爆开始 → 出锅 drop |
| 一爆持续 | 一爆开始 → 一爆结束 |
| 总时长 total | 下豆 → 出锅 |
| **发展时间比 DTR** | development / total |

边界事件缺失时指标为 `null`（不猜测），并返回每个锚点的来源以便审计。

### 双批次对比 —— 不宣称因果
两批次按开火/下豆时刻对齐叠加；风门变化前后的形态变化仅供观察，接口和界面都附带声明：
无对照、无重复、无统计检验，**不构成因果结论**。

## 快速开始

### 方式一：本地

    # 终端 1 —— API（需要先有 PostgreSQL，或用 SQLite 做本地演示）
    cd backend
    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    # 默认连接 postgresql+psycopg2://roast:roast@localhost:5432/roast
    # 仅本地无 PG 时：export DATABASE_URL="sqlite:///./dev.db"
    uvicorn app.main:app --reload --port 8000

    # 终端 2 —— 前端
    cd frontend
    npm install
    npm run dev        # http://localhost:5173 （/api 已代理到 8000）

打开页面后点 **① 生成两个合成批次**：A 批在 300 s 有关一次风门（70%→40%），B 批无风门变化，
两批均含测量噪声、不均采样、一次短失联（5 s，插值桥接）和一次长失联（56 s，断档不桥接）。

### 方式二：docker compose

    docker compose up --build
    # web: http://localhost:5173  api: http://localhost:8000/docs

## 验证（对应需求中的验收项）

    pytest                       # SQLite
    DATABASE_URL=postgresql+psycopg2://roast:roast@localhost:5432/roast pytest

界面“缺测与插值审计 · 导出可复现”面板一键完成：
1. 导出 JSON（原始采样 + 全量事件含已取代行 + 参数 + 阶段指标）；
2. 调 `/api/recompute` 从原始数据独立重算，逐指标比对（脱水/梅纳/发展/一爆/总时长/DTR）；
3. 再用翻倍窗口、不同平滑重取曲线，逐点比对原始豆温/环温**完全不变**。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/batches` | 批次列表 |
| POST | `/api/seed` | 生成两个合成批次 |
| GET | `/api/batches/{id}/series?window_s&display_smooth_s&max_gap_fill_s` | 曲线+RoR+指标 |
| GET/POST | `/api/batches/{id}/events[?include_history=true]` | 事件列表/人工修正（只追加） |
| GET | `/api/compare?a=&b=` | 双批次叠加（含非因果声明） |
| GET | `/api/batches/{id}/export` | 自包含导出（含逐点来源包、事件历史来源分组） |
| POST | `/api/recompute` | 从导出载荷独立重算全部派生指标（保留来源） |
| POST | `/api/imports` | 接收观察包入账本（待核验；非法包整包失败 422，幂等） |
| GET | `/api/imports[?status=]` | 导入账本（含失败/已应用记录与冲突计数） |
| GET | `/api/imports/{id}/preview` | 预览投影 + 冲突清单（不写库） |
| POST | `/api/imports/{id}/resolve` | 提交冲突裁决（保留已有/采用包内） |
| POST | `/api/imports/{id}/apply` | 一次性应用（单事务；重复调用返回原结果） |
| POST | `/api/imports/{id}/abort` | 放弃待核验包（账本留痕，曲线不变） |

## 目录

    backend/app/  config.py models.py analysis.py synth.py schemas.py main.py
                  importer/  package.py(解析/摘要/整包校验) planner.py(预览=应用同一合并逻辑)
                             service.py(账本/冲突/事务应用)
    frontend/src/ App.svelte lib/RoastChart.svelte lib/ImportPanel.svelte lib/api.js
    tests/        test_analysis.py test_api.py test_imports.py（双后端同一套用例）
