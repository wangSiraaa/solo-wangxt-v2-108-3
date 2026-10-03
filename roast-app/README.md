# 烘焙批次曲线对比系统

面向烘焙负责人的**过程记录**工具：并排查看豆温、环境温度与操作事件（回温点、一爆、风门变化、出锅），
而不是用成品评分替代过程。系统**不连接真实烘焙机**，数据来自带噪声、不均采样与探针失联的合成生成器，
或操作员在现场离线记录、带回工作台导入的**观察包**（本地 JSON 文件，全程不上传）。

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

### 离线观察包导入 —— 只读本地文件，待核验后一次性应用
操作员在现场用手持设备/笔记本离线记录的样本与事件可打包成 JSON **观察包**带回工作台，
浏览器用 FileReader 读取本机文件、只向同源离线后端 POST 字节，**不向任何外部主机上传**。

**包格式 v1**：

```json
{
  "format_version": 1,
  "package_id": "obs-2026-09-28-field-li-07",
  "generated_at": "2026-09-28T10:42:00",
  "batch": {"name": "FIELD-...", "roaster": "...", "bean": "...",
            "charge_at": "ISO-8601", "charge_temp_c": 180.0,
            "ambient_temp_c": 21.5, "target_drop_temp_c": 205.0},
  "samples": [{"t_s": 0.0, "bean_temp_c": 180.0, "env_temp_c": 190.0,
               "sampled_at": null}],
  "events":  [{"event_type": "turning_point", "t_s": 58.0,
               "source": "manual", "created_by": "...", "value_num": null}],
  "summary": {"n_samples": 201, "n_events": 6, "t_first_s": 0.0, "t_last_s": 600.0,
              "n_missing_bean": 23, "n_missing_env": 0, "sha256": "<64位十六进制>"}
}
```

内容摘要 `sha256` 的算法是固定的（前端“本地预检”与后端用同一套）：取文档规定的键
（样本 `t_s/bean_temp_c/env_temp_c/sampled_at`，事件 `event_type/t_s/label/source/created_by/value_num/note`，
以及 `format_version/package_id/generated_at/batch`），JSON 以 `sort_keys=True、separators=(",",":")、
ensure_ascii=False` 规范化后取 SHA-256。数组可以乱序——首末时刻按时间极值校验，入库后统一按 `t_s` 排序。

**生命周期与保证**：

1. **整包校验**：格式版本、包标识、ISO 时间、有限非负 `t_s`、温度物理范围、风门开度 0–100%、
   包内重复采样时刻、重复事件链、锚点事件时间顺序（charge→回温点→一爆→出锅）、摘要计数与 sha256——
   任一不通过则**整包失败**，只在 `observation_imports` 账本留一条 `rejected`（含错误码、详情、原始包），
   不产生任何批次/样本/事件，不存在半套曲线。
2. **待核验（pending_review）**：合法包先落账本，不写业务数据。预览接口实时投影“应用后”的
   样本集、事件集、曲线与阶段指标；预览和实际应用由同一个投影函数生成，结果必然一致。
3. **冲突与裁决**：同一时刻已有不同读数 → 样本冲突；锚点事件时间不同、同刻风门开度不同 → 事件冲突。
   冲突必须逐项裁决（`keep_existing` / `use_incoming` / `supersede`），未裁决不能应用，
   **未裁决前当前曲线与阶段分析完全不变**。采用新值时旧样本行/旧事件行置 `superseded` 并链接到新行，
   绝不删除（样本用部分唯一索引 `WHERE superseded=FALSE` 保证每个 (批次, t_s) 至多一条当前读数）。
4. **一次性原子应用**：应用在单事务内重算冲突、复核裁决、写入并提交；任何失败整体回滚。
5. **幂等投递 / 乱序到达**：以 `(package_id, sha256)` 为幂等键——完全相同的包（含失败包）重试都返回
   **原导入记录**且不新增任何数据；同一 `package_id` 不同内容拒绝（409，防稳定标识被篡改/复用）；
   合并永远针对“当前行”计算，先到/后到顺序不影响结果。
6. **来源保留**：导入样本记 `source=observation_package` + `import_package_id`，事件同样带包标识；
   曲线、导出、`/api/recompute` 独立重算都逐点/逐条回带来源；事件继续遵守只追加与 supersede 历史。

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

    pytest                       # SQLite（含 tests/test_imports.py 五个验收场景）
    DATABASE_URL=postgresql+psycopg2://roast:roast@localhost:5432/roast pytest

`tests/test_imports.py` 对应验收项：① 合法包（不均采样/缺测/人工事件）预览与应用一致、
曲线与导出来源可追溯；② 完全相同的包重试返回原结果、零新增；③ 同时刻不一致读数保持待裁决、
未裁决分析不变；④ 非法时间/重复事件链/解析错误整包失败且无残留；⑤ 刷新后重新导出并独立重算，
事件历史、样本来源与阶段指标完全一致。

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
| GET | `/api/batches/{id}/export` | 自包含导出（样本含来源/包标识、事件含全量历史） |
| POST | `/api/recompute` | 从导出载荷独立重算全部派生指标、事件历史与样本来源 |
| POST | `/api/imports` | 投递本地观察包字节（201 待核验 / 200 拒绝或重复 / 409 包标识冲突） |
| GET | `/api/imports[?status=]` | 导入账本（待核验/已应用/已拒绝/已丢弃） |
| GET | `/api/imports/{id}` | 单个导入的实时预览/冲突报告（未应用前每次按当前库重算） |
| PUT | `/api/imports/{id}/resolutions` | 保存逐项冲突裁决（不应用） |
| POST | `/api/imports/{id}/apply` | 一次性原子应用（未裁决/被他包改动则 409） |
| POST | `/api/imports/{id}/discard` | 丢弃待核验包（保留审计行） |

## 目录

    backend/app/  config.py models.py analysis.py synth.py schemas.py main.py importer.py
    frontend/src/ App.svelte lib/RoastChart.svelte lib/ImportPanel.svelte lib/api.js
    tests/        test_analysis.py test_api.py test_imports.py（双后端同一套用例）
