# FetchData

足球比赛数据抓取服务。当前通过 `titan007` 比赛列表持续发现比赛，并采集详情和
三类赔率变动。

项目明确采用单数据源实现；Titan007 的页面结构、公司 ID 和表名直接作为领域
约束维护，不提供尚无第二个实现的通用 Provider 抽象。

完整的运行流程、数据归属和当前运维约束见
[`docs/architecture/code-flow.md`](../docs/architecture/code-flow.md)。

## 当前字段

- `match_id`：比赛 ID，来自比赛行 `tr1_<id>`
- `league`：联赛/杯赛名称
- `home_team` / `away_team`：主客队
- `score`、`home_score`、`away_score`：当前比分；未开赛时为 `null`
- `status`：标准化状态
- `status_text`：页面原始状态（例如 `90+1`、`中`、`完`）
- `scheduled_time`：北京时间开赛年月日和时间，格式为 `YYYY-MM-DD HH:MM`
- `scheduled_at`：由 `scheduled_time` 转换出的 PostgreSQL `TIMESTAMPTZ`
- `source`：数据源标识

## 安装

```bash
cd FetchData
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

## 按比赛 ID 抓取赔率变化

该命令使用 `.env` 中的 `PROXY_API_URL`、`PROXY_USERNAME` 和
`PROXY_PASSWORD` 获取并验证代理，并使用 `DATABASE_URL` 连接 PostgreSQL；
运行前必须配置这些变量。

传入一个 Titan007 比赛 ID，同时抓取 6 家公司对应的亚让、胜平负和进球数变化页面：

```bash
fetch-odds 3020831
```

默认抓取公司 ID：`3`、`4`、`8`、`24`、`31`、`47`，即每场比赛请求 18 个页面。调试时可以只抓指定公司，`--company-id` 可以重复：

```bash
fetch-odds 3020831 --company-id 3
fetch-odds 3020831 --company-id 3 --company-id 8 --headed
```

并发数和页面超时可调整：

```bash
fetch-odds 3020831 --concurrency 3 --timeout 60
```

抓取结果在一个事务中写入三张赔率变化表：

- `titan007_handicap_changes`：亚让
- `titan007_1x2_changes`：胜平负
- `titan007_over_under_changes`：进球数

命令不会输出赔率 JSON，只输出本次写入的三类记录数量。重复执行时根据
`(match_id, company_id, seq)` 更新已有记录。`seq` 不使用时间排序，而是页面
底部为 1、向顶部递增。赛前的比赛分钟和比分为 `null`；封盘行的三个市场值
及变动方向为 `null`。

采集器会检查 HTTP 状态、错误/拦截页特征、市场容器和市场导航。只有确认页面
属于赔率市场、但缺少对应表格时，才把它当作合法空市场。

每家公司是一个原子采集单元：三个市场全部成功才写入该公司本轮数据。某个页面
失败时只放弃对应公司的三个市场，其他完整公司的数据仍正常写入；如果所有选中
公司都失败，整次命令失败。合法空市场不会删除数据库中的已有记录。
命令摘要会分别输出本轮成功和失败的公司 ID。

## 测试

```bash
python -m unittest discover -s tests -v
```

## 同步比赛数据到 PostgreSQL

`match_status` 只保存比赛 ID 和详情爬取状态：

```sql
CREATE TABLE match_status (
    match_id BIGINT PRIMARY KEY,
    crawl_status TEXT NOT NULL DEFAULT '未完成'
        CHECK (crawl_status IN ('未完成', '已完成')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`crawl_status` 新建时始终为“未完成”。只有同时满足以下条件才更新为“已完成”：

1. `match_basic_info.status_text = '完'`；
2. `scheduled_time` 按北京时间计算，已早于当前时间至少 3 小时；
3. 比赛完场后，公司 `3`、`4`、`8`、`24`、`31`、`47` 的亚让、胜平负和进球数本次最后一条数据，均与写入前数据库保存的上一次最后一条逐字段一致。

比赛完场且开赛已超过 3 小时后，若某市场页面为 0 条且数据库中同一比赛、公司、
市场也为 0 条，则“空对空”一致，该市场计为完成；页面为空但数据库非空则不完成。使用
`fetch-odds --company-id` 分批抓取时，只重新核验本次选择的公司；六家公司
均在完场后通过三个市场最终记录核验，才允许完成比赛。比赛完成状态只从
“未完成”变为“已完成”，不会自动回退。

若本次页面记录与上一次数据库记录不一致，本次数据仍会正常写入，但核验保持
未完成；下一次抓取结果仍相同时才通过稳定性核验。

比赛基本信息保存在 `match_basic_info`：

```sql
CREATE TABLE match_basic_info (
    match_id BIGINT PRIMARY KEY REFERENCES match_status(match_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    league TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ,
    home_score SMALLINT,
    away_score SMALLINT,
    status_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

项目创建的所有数据库表都包含 `created_at` 和 `updated_at`。前者只记录首次
插入时间；后者在记录内容更新、完成状态变化或赔率抓取尝试时刷新。

复制环境变量模板，并填写真实的 PostgreSQL 账号信息：

```bash
cp .env.example .env
```

`.env` 内容示例：

```dotenv
DATABASE_URL=postgresql://postgres:password@localhost:5432/football
PROXY_API_URL=https://proxy-supplier.example/api/getip
PROXY_USERNAME=replace-me
PROXY_PASSWORD=replace-me
```

三个代理变量均为必填项。真实代理 API 地址和认证信息只应保存在本地
`.env` 或部署环境的密钥配置中，不要提交到仓库。采集器会先从代理 API
获取代理后先验证其可用性，验证通过才启动 Playwright 并访问 Titan007；
代理默认每 60 秒更新，连续 3 次访问失败时会提前更新。可选配置如下：

```dotenv
PROXY_UPDATE_INTERVAL=60
PROXY_MAX_CONSECUTIVE_ERRORS=3
PROXY_TEST_URL=https://live.nowscore.com
PROXY_API_TIMEOUT=5
PROXY_TEST_TIMEOUT=5
```

`.env` 已被 Git 忽略。安装后启动同步进程：

```bash
sync-match-status
```

同步进程内部运行三个互不等待的任务：

- 列表任务默认每 60 秒抓取比赛列表，向 `match_status` 补充比赛 ID，并更新 `match_basic_info` 中的开赛时间、比分和当前状态。
- 详情任务默认每 60 秒查询 `crawl_status = '未完成'` 的比赛，从皇冠简体名称页 `https://live.titan007.com/detail/{match_id}sb.htm` 抓取主客队和联赛等信息；默认每 10 场一批，每批完成立即入库。
- 赔率任务默认每 5 秒查询赔率最终记录尚未完成核验的比赛；每轮默认取 1 场，抓取 6 家公司 × 3 个市场并写入三张赔率表。队列优先选择从未抓取或最久未尝试刷新的比赛；抓取失败也会记录尝试时间，避免坏页面长期占住队首。

进程启动时会获取 PostgreSQL advisory lock，同一数据库只允许一个
`sync-match-status` 进程运行。

三个间隔可独立调整：

```bash
sync-match-status --list-refresh-seconds 60 --detail-refresh-seconds 120 \
  --detail-batch-size 10 --odds-refresh-seconds 5 --odds-batch-size 1
```

每个任务的间隔都从该轮结束后开始计算。例如赔率抓取和写库耗时 8 秒时，
默认下一轮会在约 13 秒后开始，并非严格每 5 秒发起一次。

## 健康检查与指标

`sync-match-status` 默认在 `127.0.0.1:8080` 提供：

- `GET /healthz`：数据库、代理和三个采集任务的最近健康状态；正常返回 200，
  启动中或降级返回 503。
- `GET /metrics`：Prometheus 文本指标，包括任务耗时与成功/失败次数、页面成功率、
  详情和赔率待处理数量、代理刷新/验证/失效次数、赔率公司部分失败情况。

```bash
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/metrics
```

监听地址和端口可配置；端口设为 0 可关闭 HTTP 端点：

```bash
sync-match-status --health-host 0.0.0.0 --health-port 8080
sync-match-status --health-port 0
```

## 数据库迁移

数据库定义的唯一来源是 `fetch_data/migrations/*.sql`。这些 SQL 文件作为包数据
随 wheel 发布，运行时存储模块只负责按顺序加载并执行，不再维护 Python DDL 副本。

## 页面分析结论

`oldIndexall.aspx` 的初始比赛数组由页面脚本加载，随后通过 WebSocket 更新；最终渲染出的比赛行结构稳定：

| DOM | 含义 |
| --- | --- |
| `tr[id^="tr1_"]` | 一场比赛，后缀为比赛 ID |
| 第 2 列（索引 1） | 联赛 |
| 第 4 列（索引 3） | 状态 |
| 第 5 列（索引 4） | 主队 |
| 第 6 列（索引 5） | 比分 |
| 第 7 列（索引 6） | 客队 |

抓取器读取渲染后的 DOM，因此能获得页面当时的实时比分，又不依赖站点内部压缩协议。
