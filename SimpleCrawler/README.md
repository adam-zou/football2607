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
| 发现比赛 ID | 15 分钟 | `fetch_match_ids.py` |
| 抓取比赛详情 | 5 秒 | `fetch_match_details.py` |
| 抓取赔率变化 | 5 秒 | `fetch_odds_pages.py` |
| 核验完场数据 | 60 秒 | `check_match_completion.py` |

这里的间隔是上一轮结束后到下一轮开始前的等待时间。按 `Ctrl+C` 可以停止调度器
及其启动的子进程。

比赛 ID 任务首次页面抓取失败后，会在同一轮立即隔离当前代理并更换代理重试
两次，因此每轮最多尝试三次。任意一次成功即停止重试；三次全部失败后才退出
本轮，并等待默认的 15 分钟任务间隔。

比赛详情采用相同策略。每场比赛首次详情页面抓取失败后，会在当前详情并发槽位
内立即更换代理重试两次；其他比赛的并发任务不受影响。三次全部失败才把该场
计入本轮失败。

```text
[比赛 ID] 第 1/3 次获取失败：代理空响应
[比赛 ID] 切换代理，开始第 2/3 次尝试。
[比赛详情] 2999890 | 第 1/3 次获取失败：等待详情头部超时
[比赛详情] 2999890 | 切换代理，开始第 2/3 次尝试。
```

总调度器启动后会同时提供本地监控页面：

```text
http://127.0.0.1:8081/
```

页面分窗口显示代理服务、比赛 ID、比赛详情、赔率变化和完成核验的运行状态、
最近一轮耗时、退出码、下轮时间及滚动日志。每个窗口最多保留最近 400 行日志，
浏览器每秒更新一次；终端仍会同步输出完整的实时日志。页面顶部集中提示任务、
数据库、代理和数据质量告警，并展示待获取详情、当前赔率队列、完场待核验、
待补最终页面及最早积压时间。代理池健康状态每 10 秒结构化刷新，包括当前、
可用、租用、隔离代理、页面槽位、最近获取和验证通过率；同一摘要也会写入日志。
页面顶部还会按上海时区开赛日期展示今日比赛 ID 总数、根据 `status_text` 识别的
完场及进行中数量、按 `crawl_status` 统计的今日已完成/异常/暂停爬取数量、今日
之前的历史比赛总数及未完成数，以及六家公司在亚让、胜平负和进球数市场中的
赔率变动记录总数。已有详情但 `scheduled_time` 不符合 `YYYY-MM-DD HH:MM` 格式
的比赛单独计入“时间异常”。监控页将今日和历史比赛分栏，并分别完整展示
未开始、进行中、完场、推迟、取消、待定、其他状态，以及未完成、已完成、
暂停爬取和异常四类爬取状态。今日和历史栏还会把“未完成”按上述七种比赛状态
分别列出；缺少详情的比赛只计入顶部的数据质量统计。
问题比赛表最多列出 20 场缺少详情、时间异常、完场未完成、暂停、异常或存在页面
采集错误的比赛，并显示最近页面错误。以上数据库统计每 10 秒刷新一次。
比赛详情、赔率变化和完成核验窗口还会显示各自本轮选中的比赛数量；没有任务时
显示 0 场，新一轮开始后会重新计算。

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

列表页保留 Chromium 的网络指纹，但不再等待或查询渲染后的比赛表格。
脚本阻断图片、样式、媒体和字体，只等待 `bfdata_ut.js` 数据响应并直接解析
其中的比赛 ID；响应声明的 `matchcount` 必须与唯一 ID 数量一致，否则本次
代理视为失败并立即更换。`SIMPLE_CRAWLER_LIST_SETTLE_SECONDS` 仅为旧配置兼容
保留，当前响应解析模式不会额外等待。

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

不指定比赛 ID 时，普通详情轮次优先补齐缺失详情，并只刷新开赛前
30 分钟至开赛后 4 小时内、距上次更新至少 1 分钟且页面状态不是
`完` 的比赛。已有详情但开赛时间无法解析的比赛会按 1 分钟节奏重试。
显式传入比赛 ID 表示强制刷新，不检查 `crawl_status`，也不受上述时间窗口
和 1 分钟节奏限制。详情页仍保留脚本执行和 DOM 读取，
以获取动态比分及状态。

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

普通赔率任务只选择已取得比赛详情、且上海时区开赛时间位于当前时间
前 4 小时至后 30 分钟内的比赛。这个限制也适用于命令行显式传入的比赛 ID。
队列优先级为：进行中 → 未来 30 分钟 → 最近完场。详情状态变为“完”后，
普通赔率继续刷新 5 分钟，随后停止选择该比赛并交给完成核验。页面仍由 Chromium 和代理
打开以保留可用的网络指纹，但赔率表直接从主文档响应 HTML 解析，不等待浏览器
渲染 DOM。赔率页的脚本、图片、样式、媒体和字体请求全部阻断；这些资源不参与
服务器已生成的赔率表。异步任务粒度是“比赛 × 公司”：同一任务只领取一次代理并创建一个
Chromium context，随后依次读取亚让、胜平负和进球数三个市场。租约仍按实际
页面数消耗三个代理页面额度，不会因 context 复用绕过每个 IP 的调用上限。
公司任务领取代理时只要求代理当前尚未过期，不再用三个页面的最大超时总和
估算最低剩余寿命；代理若在处理中失效，已成功市场仍会保留，失败市场更换
代理重试。
普通赔率任务与完成核验共用 `odds_collection.py`：该模块统一处理页面
身份、URL、代理租约、Chromium context、首个 HTTP 响应校验、HTML 解析和
赔率变动记录解析。普通任务和最终快照任务都通过它写入赔率与页面状态。
写入数据库时，每条批量 `INSERT ... ON CONFLICT` 最多携带 500 行；已存在的
历史行只有在比分、盘口、赔率、变动方向或源状态实际变化时才更新，
完全相同的行不会重写 `updated_at`。

每次页面处理还会在 `titan007_odds_market_state` 中保存一条“比赛 × 公司
× 市场”状态：最后尝试和成功时间、成功/失败状态、赔率变动记录数量、
解析内容的 SHA-256 摘要以及最后错误。失败会保留上一次成功的数量、
摘要和时间。`final_required` 和 `final_success_at` 记录每个页面的最终快照进度。

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
[完成核验] 2955275 | 已完成 | 全部最终快照页面已成功写入
```

### 收尾稳定完场或开赛超过 4 小时的比赛

```bash
python SimpleCrawler/check_match_completion.py
```

限制单轮收尾数量：

```bash
python SimpleCrawler/check_match_completion.py --limit 20
```

收尾任务固定采集 6 家公司 × 3 个市场，共 18 页，不受普通赔率公司配置
缩减。每家公司首先用一个 context 依次读取尚未完成的市场；失败市场最多
尝试三次，重试时不会再次请求同公司的成功市场。每场共享 180 秒总超时。
详情状态为“完”且该详情已稳定至少 5 分钟的比赛会立即进入收尾；尚未显示
“完”的比赛在开赛超过 4 小时后仍会进入收尾，并先强制刷新一次详情作为兜底。
其中状态为“推迟”“取消”或“待定”的比赛，自详情最后更新时间起等待 7 天后才会再次
进入收尾核验；核验刷新后若状态仍未变，新的 7 天等待期从该次刷新开始计算。
刷新后会重新读取详情状态；仍不是“完”时保留 `未完成`，不采集最终赔率。
成功页面会立即写入，并按数据库中的累计最终成功页数归类：0～3 页为
`未完成`，4～6 页为 `异常`，7～17 页为 `暂停爬取`，18 页为 `已完成`。
只有 `未完成` 会进入下一轮自动核验；其余状态必须人工重置为 `未完成` 才会重跑。

```dotenv
SIMPLE_CRAWLER_COMPLETION_MATCH_TIMEOUT_SECONDS=180
```

```bash
python SimpleCrawler/check_match_completion.py --match-timeout 180
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
| `SIMPLE_CRAWLER_ODDS_PAGE_CONCURRENCY` | `12` | 同时抓取的“比赛 × 公司”任务数量 |
| `SIMPLE_CRAWLER_ODDS_COMPANY_IDS` | `3,4,8,24,31,47` | 要抓取的公司 ID |
| `SIMPLE_CRAWLER_ID_INTERVAL_SECONDS` | `900` | 比赛 ID 任务间隔 |
| `SIMPLE_CRAWLER_DETAIL_INTERVAL_SECONDS` | `5` | 详情任务间隔 |
| `SIMPLE_CRAWLER_ODDS_INTERVAL_SECONDS` | `5` | 赔率任务间隔 |
| `SIMPLE_CRAWLER_COMPLETION_INTERVAL_SECONDS` | `60` | 完成核验任务间隔 |
| `SIMPLE_CRAWLER_COMPLETION_MATCH_CONCURRENCY` | `2` | 同时进入最终核验的比赛数；共享赔率公司任务并发上限 |
| `SIMPLE_CRAWLER_COMPLETION_MATCH_TIMEOUT_SECONDS` | `180` | 单场最终快照总超时 |
| `SIMPLE_CRAWLER_MONITOR_HOST` | `127.0.0.1` | 监控页面监听地址 |
| `SIMPLE_CRAWLER_MONITOR_PORT` | `8081` | 监控页面端口；`0` 表示关闭 |
| `PROXY_SCHEDULER_URL` | `http://127.0.0.1:8765` | 统一代理服务地址 |
| `PROXY_REFRESH_SECONDS` | `1.6` | 代理池刷新间隔 |
| `PROXY_API_MIN_INTERVAL_SECONDS` | `1.6` | 跨代理服务进程共享的供应商最小请求间隔 |
| `PROXY_TTL_SECONDS` | `30` | 代理 IP 有效期 |
| `PROXY_MAX_PAGE_ASSIGNMENTS_PER_IP` | `5` | 每个代理 IP 最多分配的页面调用数量；公司任务按市场数预占 |
| `PROXY_RETIRE_SECONDS` | `3600` | 页面失败或用满额度后的地址隔离时间；到期后仍需供应商重新提供并验证 |
| `PROXY_ACQUIRE_TIMEOUT_SECONDS` | `5` | 等待可用代理的最长时间 |
| `PROXY_TEST_URL` | `https://live.titan007.com/oldIndexall.aspx` | 新代理入池前的验证地址 |
| `PROXY_TEST_TIMEOUT_SECONDS` | `5` | 单个代理验证请求超时 |

所有间隔、超时和并发配置必须是有效的正数。需要禁用单轮数量限制时，将对应
`LIMIT` 配置留空。

## 爬取状态

`match_ids.crawl_status` 支持以下状态：

| 状态 | 含义 |
| --- | --- |
| `未完成` | 详情尚未完场，或最终成功 0～3 页，继续自动核验 |
| `已完成` | 18 个最终快照页面全部成功写入 |
| `暂停爬取` | 最终成功 7～17 页，停止自动核验 |
| `异常` | 最终成功 4～6 页，停止自动核验 |

完成核验固定只选择 `未完成`。`暂停爬取` 和 `异常` 不会自动重新进入核验；
需要重跑时，先人工重置该比赛及目标页面状态。普通详情和赔率任务仍由
`SIMPLE_CRAWLER_ACTIVE_CRAWL_STATUSES` 控制，默认也只处理 `未完成`。例如临时让
普通任务处理异常比赛：

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
