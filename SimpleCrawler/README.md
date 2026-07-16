# SimpleCrawler

SimpleCrawler 是一组独立运行的 Titan007 足球比赛采集脚本。它使用
Playwright 访问比赛列表、比赛详情和赔率变化页面，并将结果保存到 PostgreSQL。

它会完成以下工作：

- 从比赛列表页发现比赛 ID；
- 抓取联赛、主队、客队、开赛时间、比分和比赛状态；
- 抓取指定公司的亚让、胜平负和进球数赔率变化；
- 核验完场比赛的数据是否完整，并更新爬取状态；
- 统一获取、验证和分配短效代理 IP。

> SimpleCrawler 只负责采集、保存数据和运行监控，不提供预测或数据分析功能。

## 运行要求

- Python 3.9 或更高版本；
- PostgreSQL；
- Chromium（通过 Playwright 安装）；
- 能返回 `host:port` 的代理供应商 API；
- 可用的代理用户名和密码。

## 快速开始

下面的命令均假设当前目录是仓库根目录：

```bash
cd /Users/adam/Documents/AdamSpace/football2607
```

### 1. 安装 Python 依赖

SimpleCrawler 拥有独立的 Python 包、赔率模型和页面解析代码。直接安装
`SimpleCrawler` 即可获得运行所需的全部 Python 依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./SimpleCrawler
playwright install chromium
```

上面的命令从仓库根目录执行。如果当前已经位于 `SimpleCrawler` 目录，则安装
当前目录：

```bash
python -m pip install -e .
```

`-e` 后面的路径相对于当前工作目录；不要在 `SimpleCrawler` 目录中再次写
`./SimpleCrawler`，否则会被解释为不存在的 `SimpleCrawler/SimpleCrawler`。

之后重新打开终端时，需要先激活虚拟环境：

```bash
source .venv/bin/activate
```

### 2. 创建数据库

数据库必须在启动爬虫前存在。例如创建名为 `football_simple` 的数据库：

```bash
createdb football_simple
```

如果数据库已经存在，可以跳过此步骤。数据表由各脚本在首次运行时自动创建。

### 3. 填写配置

如果还没有配置文件，复制示例：

```bash
cp SimpleCrawler/.env.example SimpleCrawler/.env
```

编辑 `SimpleCrawler/.env`，至少填写以下配置：

```dotenv
SIMPLE_CRAWLER_DATABASE_URL=postgresql://postgres:你的密码@localhost:5432/football_simple

PROXY_API_URL=你的代理供应商取IP接口
PROXY_USERNAME=你的代理用户名
PROXY_PASSWORD=你的代理密码
```

代理 API 应返回纯文本代理地址，每行一个 `host:port`。代理认证信息由
Playwright 在访问目标页面时使用。

不要提交 `.env`。仓库已经忽略 `SimpleCrawler/.env`。

### 4. 启动完整采集流程

```bash
python SimpleCrawler/run_scheduler.py
```

总调度器会自动启动统一代理服务，并并行调度四类任务：

| 任务 | 默认间隔 | 脚本 |
| --- | ---: | --- |
| 发现比赛 ID | 60 秒 | `fetch_match_ids.py` |
| 抓取比赛详情 | 5 秒 | `fetch_match_details.py` |
| 抓取赔率变化 | 5 秒 | `fetch_odds_pages.py` |
| 核验完场数据 | 60 秒 | `check_match_completion.py` |

这里的间隔是上一轮结束后到下一轮开始前的等待时间。按 `Ctrl+C` 可以停止调度器
及其启动的子进程。

总调度器启动后会同时提供本地监控页面：

```text
http://127.0.0.1:8081/
```

页面分窗口显示代理服务、比赛 ID、比赛详情、赔率变化和完成核验的运行状态、
最近一轮耗时、退出码、下轮时间及滚动日志。每个窗口最多保留最近 400 行日志，
浏览器每秒更新一次；终端仍会同步输出完整的实时日志。代理服务窗口每 10 秒
追加一次代理池健康摘要，包括当前代理、租用、可用代理、页面槽位和最近验证数量。

调度器使用文件锁保证同一时间只有一个实例运行。如果重复启动，会输出：

```text
SimpleCrawler 调度器已经在运行
```

## 单独运行脚本

日常运行推荐使用总调度器。调试或补采时，可以单独启动任务。

### 启动代理调度服务

单独执行采集脚本前，先在一个终端启动统一代理服务：

```bash
python SimpleCrawler/proxy_scheduler.py
```

默认监听 `http://127.0.0.1:8765`，可用以下命令检查状态：

```bash
curl http://127.0.0.1:8765/health
```

### 获取比赛 ID

```bash
python SimpleCrawler/fetch_match_ids.py
```

显示浏览器窗口以便调试：

```bash
python SimpleCrawler/fetch_match_ids.py --headed
```

### 获取比赛详情

不传比赛 ID 时，从数据库读取所有符合爬取状态的比赛：

```bash
python SimpleCrawler/fetch_match_details.py
```

抓取一场或多场指定比赛：

```bash
python SimpleCrawler/fetch_match_details.py 3020831
python SimpleCrawler/fetch_match_details.py 3020831 3020832
```

限制数量或调整并发：

```bash
python SimpleCrawler/fetch_match_details.py --limit 20 --concurrency 2
```

### 获取赔率变化

处理数据库中的待抓取比赛：

```bash
python SimpleCrawler/fetch_odds_pages.py
```

只抓取指定比赛和指定公司：

```bash
python SimpleCrawler/fetch_odds_pages.py 3020831 --company-id 3
```

可以重复传入 `--company-id`：

```bash
python SimpleCrawler/fetch_odds_pages.py 3020831 \
  --company-id 3 \
  --company-id 4
```

支持的公司 ID 为 `3`、`4`、`8`、`24`、`31`、`47`。每家公司会抓取亚让、
胜平负和进球数三个市场。

日志同时显示公司 ID 和名称。名称映射由 SimpleCrawler 本地维护：

| 公司 ID | 名称 |
| ---: | --- |
| 3 | `Crow*` |
| 4 | `立*` |
| 8 | `36*` |
| 24 | `12*` |
| 31 | `利*` |
| 47 | `平*` |

所有任务日志都带有任务前缀，例如：

```text
[比赛 ID] 共获取 200 个比赛 ID，数据库新增 3 个。
[比赛详情] 2910816 | 巴西甲 | 维多利亚BA - 华斯高RJ | 2026-07-17 06:30 | 未开始
[赔率] 2908632 | 公司 3（Crow*） | 亚让 | 44 条
[完成核验] 2955275 | 未完成 | 公司 3（Crow*） 亚让赔率变动记录数不一致
```

### 核验完场比赛

```bash
python SimpleCrawler/check_match_completion.py
```

限制单轮核验数量：

```bash
python SimpleCrawler/check_match_completion.py --limit 20
```

查看任一脚本的全部命令行参数：

```bash
python SimpleCrawler/fetch_odds_pages.py --help
```

## 主要配置

完整配置和注释见 `.env.example`。常用配置如下：

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `SIMPLE_CRAWLER_DATABASE_URL` | 无 | PostgreSQL 连接地址，必填 |
| `SIMPLE_CRAWLER_ACTIVE_CRAWL_STATUSES` | `未完成` | 允许进入采集任务的状态，英文逗号分隔 |
| `SIMPLE_CRAWLER_HEADED` | `false` | 是否显示 Chromium 窗口 |
| `SIMPLE_CRAWLER_DETAIL_LIMIT` | 不限制 | 单轮最多抓取多少场详情 |
| `SIMPLE_CRAWLER_DETAIL_CONCURRENCY` | `2` | 同时抓取的详情页数量 |
| `SIMPLE_CRAWLER_ODDS_MATCH_LIMIT` | 不限制 | 单轮最多处理多少场赔率 |
| `SIMPLE_CRAWLER_ODDS_PAGE_CONCURRENCY` | `4` | 同时抓取的赔率页面数量 |
| `SIMPLE_CRAWLER_ODDS_COMPANY_IDS` | `3,4,8,24,31,47` | 要抓取的公司 ID |
| `SIMPLE_CRAWLER_ID_INTERVAL_SECONDS` | `60` | 比赛 ID 任务间隔 |
| `SIMPLE_CRAWLER_DETAIL_INTERVAL_SECONDS` | `5` | 详情任务间隔 |
| `SIMPLE_CRAWLER_ODDS_INTERVAL_SECONDS` | `5` | 赔率任务间隔 |
| `SIMPLE_CRAWLER_COMPLETION_INTERVAL_SECONDS` | `60` | 完成核验任务间隔 |
| `SIMPLE_CRAWLER_MONITOR_HOST` | `127.0.0.1` | 监控页面监听地址 |
| `SIMPLE_CRAWLER_MONITOR_PORT` | `8081` | 监控页面端口；`0` 表示关闭 |
| `PROXY_SCHEDULER_URL` | `http://127.0.0.1:8765` | 统一代理服务地址 |
| `PROXY_REFRESH_SECONDS` | `2` | 代理池刷新间隔 |
| `PROXY_TTL_SECONDS` | `30` | 代理 IP 有效期 |

所有间隔、超时和并发配置必须是有效的正数。需要禁用单轮数量限制时，将对应
`LIMIT` 配置留空。

## 爬取状态

`match_ids.crawl_status` 支持以下状态：

| 状态 | 含义 |
| --- | --- |
| `未完成` | 数据仍需继续抓取或核验 |
| `已完成` | 完场数据与远端页面核验一致 |
| `暂停爬取` | 已超过继续抓取的时间范围，暂停处理 |
| `异常` | 最终抓取中有较多页面失败，需要后续排查 |

默认只有 `未完成` 会进入详情、赔率和完成核验任务。如果需要重新处理其他状态，
可以临时修改：

```dotenv
SIMPLE_CRAWLER_ACTIVE_CRAWL_STATUSES=未完成,异常
```

## 数据表

脚本会按需创建或升级以下主要数据表：

- `match_ids`：比赛 ID 和爬取状态；
- `match_details`：联赛、球队、时间、比分和比赛状态；
- `titan007_handicap_changes`：亚让变化；
- `titan007_1x2_changes`：胜平负变化；
- `titan007_over_under_changes`：进球数变化。

可以使用 PostgreSQL 查看采集结果：

```bash
psql football_simple
```

```sql
SELECT crawl_status, COUNT(*)
FROM match_ids
GROUP BY crawl_status
ORDER BY crawl_status;

SELECT *
FROM match_details
ORDER BY match_id DESC
LIMIT 10;
```

## 运行测试

激活虚拟环境后，在仓库根目录执行：

```bash
PYTHONPATH=SimpleCrawler \
  python -m unittest discover -s SimpleCrawler/tests -v
```

测试使用模拟对象，不会访问真实代理供应商或 Titan007 页面。

## 常见问题

### 提示缺少 `SIMPLE_CRAWLER_DATABASE_URL`

确认配置文件位于 `SimpleCrawler/.env`，并且连接地址对应的 PostgreSQL 数据库
已经创建。

### 无法连接代理调度器

使用总调度器时，代理服务会自动启动。单独运行采集脚本时，需要先运行：

```bash
python SimpleCrawler/proxy_scheduler.py
```

然后检查：

```bash
curl http://127.0.0.1:8765/health
```

### Chromium 找不到或无法启动

在当前虚拟环境中重新安装浏览器：

```bash
playwright install chromium
```

### 页面抓取超时

先使用 `--headed` 查看页面是否正常加载，并检查代理是否可用。必要时再提高 `.env`
中的列表、详情或赔率超时。代理有效期默认只有 30 秒，不应将单页超时设置得接近
或超过代理有效期。

### 数据库中没有比赛 ID

先运行比赛 ID 发现任务：

```bash
python SimpleCrawler/fetch_match_ids.py
```

确认成功写入 `match_ids` 后，再运行详情和赔率任务。
