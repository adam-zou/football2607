# FetchData

可扩展的足球比赛数据抓取目录。当前实现 `titan007` 比赛列表，后续数据源通过实现
`MatchProvider` 接口接入，并统一输出 `Match` 模型。

完整的运行流程、数据归属和当前运维约束见
[`docs/architecture/code-flow.md`](../docs/architecture/code-flow.md)。

## 当前字段

- `match_id`：比赛 ID，来自比赛行 `tr1_<id>`
- `league`：联赛/杯赛名称
- `home_team` / `away_team`：主客队
- `score`、`home_score`、`away_score`：当前比分；未开赛时为 `null`
- `status`：标准化状态
- `status_text`：页面原始状态（例如 `90+1`、`中`、`完`）
- `scheduled_time`：页面显示的开赛时间
- `source`：数据源标识

## 安装

```bash
cd FetchData
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

## 使用

抓取一次比赛列表并输出 JSON：

```bash
fetch-matches --source titan007
```

如果无头模式被站点拦截，可用可视模式诊断：

```bash
fetch-matches --source titan007 --headed
```

## 按比赛 ID 抓取赔率变化

该命令使用 `.env` 中的 `PROXY_API_URL`、`PROXY_USERNAME` 和
`PROXY_PASSWORD` 获取并验证代理；运行前必须配置这些变量。

传入一个 Titan007 比赛 ID，同时抓取 6 家公司对应的亚让、胜平负和进球数变化页面：

```bash
fetch-odds 3020831 > odds-3020831.json
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

输出是一个 JSON 对象，三个数组分别对应三张赔率变化表：

- `handicap_changes`：亚让
- `one_x_two_changes`：胜平负
- `over_under_changes`：进球数

每个数组保持网页从上到下的 DOM 顺序。`seq` 不使用时间排序，而是页面底部为 1、向顶部递增。赛前的比赛分钟和比分为 `null`；封盘行的三个市场值及变动方向为 `null`。

某家公司没有提供某个市场时，Titan007 页面不会渲染赔率表；对应数组中不会产生该公司记录，其他公司和市场仍正常返回。

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
        CHECK (crawl_status IN ('未完成', '已完成'))
);
```

`crawl_status` 新建时始终为“未完成”。“已完成”的判定和更新逻辑尚未实现。

比赛基本信息保存在 `match_basic_info`：

```sql
CREATE TABLE match_basic_info (
    match_id BIGINT PRIMARY KEY REFERENCES match_status(match_id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    league TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    home_score SMALLINT,
    away_score SMALLINT,
    status_text TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

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
获取并检测代理，然后强制 Playwright 通过该代理访问 Titan007；代理默认每
300 秒更新，连续 3 次访问失败时会提前更新。可选配置如下：

```dotenv
PROXY_UPDATE_INTERVAL=300
PROXY_MAX_CONSECUTIVE_ERRORS=3
PROXY_TEST_URL=https://live.nowscore.com
PROXY_API_TIMEOUT=5
PROXY_TEST_TIMEOUT=5
```

`.env` 已被 Git 忽略。安装后启动同步进程：

```bash
sync-match-status
```

同步进程内部运行两个互不等待的任务：

- 列表任务默认每 60 秒抓取比赛列表，向 `match_status` 补充比赛 ID，并更新 `match_basic_info` 中的开赛时间、比分和当前状态。
- 详情任务默认每 60 秒查询 `crawl_status = '未完成'` 的比赛，从皇冠简体名称页 `https://live.titan007.com/detail/{match_id}sb.htm` 抓取主客队和联赛等信息；默认每 10 场一批，每批完成立即入库。

进程启动时会获取 PostgreSQL advisory lock，同一数据库只允许一个
`sync-match-status` 进程运行。

两个间隔可独立调整：

```bash
sync-match-status --list-refresh-seconds 60 --detail-refresh-seconds 120 \
  --detail-batch-size 10
```

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
