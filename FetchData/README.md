# FetchData

FetchData 是一个足球比赛与赔率历史采集服务。它从 Titan007 持续获取比赛，
并把数据保存到 PostgreSQL，适合用于赔率分析、比赛回溯和后续的数据建模。

## 它会做什么

运行后，服务会自动执行四类任务：

- 从比赛列表页发现新的比赛 ID；
- 获取联赛、主队、客队等基础详情；
- 根据数据库任务范围更新开赛时间、比赛状态和比分；
- 获取 6 家公司的亚让、胜平负和进球数赔率变化。

数据会写入 PostgreSQL。服务同时提供一个本地状态页，可以查看采集健康状态、
待处理数量和成功/失败统计。

> 本项目不是预测程序，也不提供网页前端；它的作用是采集并保存原始比赛和赔率数据。

## 最简单的使用方式

### 1. 准备环境

需要：

- Python 3.9 或更高版本；
- 一个已经运行的 PostgreSQL 数据库；
- 可用的代理供应商 API、代理用户名和密码。

先创建数据库，例如数据库名为 `football`：

```bash
createdb football
```

如果数据库已经存在，可以跳过这一步。

### 2. 安装项目

在仓库根目录执行：

```bash
cd FetchData
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
playwright install chromium
```

Windows PowerShell 激活虚拟环境时使用：

```powershell
.venv\Scripts\Activate.ps1
```

### 3. 填写配置

```bash
cp .env.example .env
```

编辑 `.env`：

```dotenv
DATABASE_URL=postgresql://postgres:你的密码@localhost:5432/football
PROXY_API_URL=你的代理供应商取 IP 接口
PROXY_USERNAME=你的代理用户名
PROXY_PASSWORD=你的代理密码
```

这 4 项都是必需的。真实密码只保存在 `.env` 中，不要提交到 Git。

### 4. 启动

```bash
sync-match-status
```

看到日志持续输出即表示服务正在运行。首次启动会自动创建或升级数据库表，
不需要手动执行 SQL。按 `Ctrl+C` 停止服务。

启动后可打开：

- 状态页：<http://127.0.0.1:8080/>
- 健康检查：<http://127.0.0.1:8080/healthz>
- Prometheus 指标：<http://127.0.0.1:8080/metrics>

这就是日常使用所需的全部步骤。

## 单独抓取一场比赛

已知 Titan007 比赛 ID 时，可以手动抓取该场比赛的赔率变化：

```bash
fetch-odds 3020831
```

默认会抓取公司 ID `3`、`4`、`8`、`24`、`31`、`47` 的三个赔率市场，
共请求 18 个页面，并把成功页面直接写入 PostgreSQL。每个“机构 × 市场”页面
独立保存；一个页面失败不会丢弃同机构另外两个已经成功的市场。

调试时可以只抓一家机构，或显示浏览器窗口：

```bash
fetch-odds 3020831 --company-id 3
fetch-odds 3020831 --company-id 3 --headed
```

## `sync-match-status` 参数说明

不传参数即可按默认值启动：

```bash
sync-match-status
```

所有可配置参数如下：

| 参数 | 默认值 | 作用 |
| --- | ---: | --- |
| `--database-url` | 读取 `DATABASE_URL` | PostgreSQL 连接地址；通常放在 `.env`，无需写在命令里 |
| `--list-refresh-seconds` | 60 秒 | 一轮比赛列表抓取完成后，等待多久再发现新比赛 ID |
| `--detail-refresh-seconds` | 60 秒 | 一轮比赛详情任务完成后，等待多久再检查缺少基础信息的比赛 |
| `--detail-batch-size` | 10 场 | 比赛详情每抓完多少场就立即写入一次数据库；它不是并发数 |
| `--dynamic-refresh-seconds` | 5 秒 | 没有到期动态信息任务时，等待多久再检查数据库队列 |
| `--dynamic-batch-size` | 10 场 | 每次从数据库领取多少场动态信息任务 |
| `--odds-refresh-seconds` | 5 秒 | 没有到期赔率任务时，等待多久再检查队列；有任务时不会每场等待 5 秒 |
| `--odds-batch-size` | 6 场 | 本地赔率队列空缺时，一次从数据库读取多少场；它不是并发数，也不会要求 6 场全部结束后才继续 |
| `--odds-match-concurrency` | 3 场 | 最多同时运行多少场完整赔率采集；每场包含 6 家公司 × 3 个市场 |
| `--odds-match-timeout-seconds` | 60 秒 | 一场比赛从获取代理、启动浏览器到完成 18 个页面的总超时；不包含数据库写入时间 |
| `--odds-page-concurrency` | 12 页 | 所有正在采集的比赛合计最多同时访问多少个赔率页面 |
| `--health-host` | `127.0.0.1` | 状态页、健康检查和 Prometheus 指标的监听地址 |
| `--health-port` | `8080` | 状态页端口；设置为 `0` 可完全关闭 HTTP 端点 |
| `--headed` | 关闭 | 显示 Playwright 浏览器窗口，用于排查页面或反爬问题 |

### 赔率数量和并发的关系

一场比赛固定采集 6 家公司，每家公司访问亚让、胜平负和进球数三个市场，
因此完整一场需要访问 18 个页面。

默认配置的含义是：

```text
数据库每次补充：6 场
同时采集比赛：3 场
全局同时访问：最多 12 页
单场总超时：60 秒
```

例如本地队列中有比赛 A～F，程序先运行 A、B、C。A 完成后立即开始 D，
不需要等待 B、C 完成。虽然三场一共会产生 54 个页面任务，但任意时刻最多只有
12 个页面访问 Titan007。某一场很慢只占用一个比赛名额，不会阻塞另外两个名额；
超过单场总超时后会被取消并进入失败退避。

页面级超时与单场总超时是两层保护：

- 比赛列表页面超时：10 秒；
- 比赛详情单页面超时：10 秒；
- 赔率单页面超时：10 秒；
- 一场比赛全部 18 个赔率页面的总超时：60 秒。

某一个赔率页面超过 10 秒时，只有该机构的该市场会失败并进入重试，其他成功
页面立即保存；整场采集即使仍有其他页面在运行，达到 60 秒也会全部取消。
常驻命令目前只允许通过
`--odds-match-timeout-seconds` 调整整场总超时，三个页面级超时固定为 10 秒。
单场补采命令可以通过 `fetch-odds --timeout` 调整赔率页面超时。

### 实测推荐配置

当前代理实测中，单独一场完整采集约需 10～24 秒；三场并发、全局 12 页时，
单场曾达到 50～80 秒。当前使用 60 秒总超时，并建议降低并发以减少代理压力：

```bash
sync-match-status \
  --odds-match-concurrency 2 \
  --odds-page-concurrency 8 \
  --odds-match-timeout-seconds 60
```

这表示同时处理 2 场、所有比赛合计最多访问 8 个赔率页面，并允许单场最多运行
60 秒。运行稳定后，再根据状态页中的失败率和队列积压逐步提高并发；如果仍频繁
触发单场总超时，可以提高到 90～120 秒。

### 赔率调度和重试规则

赔率队列按比赛阶段自动调度：

- 距离开赛超过 24 小时的比赛暂不采集赔率；
- 开赛前 5 分钟内和正在进行的比赛，成功后 1 分钟可再次采集；
- 已完场但开赛尚未超过 3 小时的比赛暂停赔率采集；
- 已完场且开赛超过 3 小时的比赛优先进行最终赔率核验；
- 每个“机构 × 市场”页面独立记录成功、失败和下次采集时间；
- 页面失败先按 1、2、5 分钟快速重试；该页面连续失败第 4 次起标记异常并每 3 小时重试，页面成功后自动解除异常并清零；
- 重试时只抓失败页面，不重复抓同机构已经成功的另外两个市场；
- 连续 3 场比赛所有领取页面都失败时，立即丢弃旧代理并重新获取、验证代理。部分页面成功不触发该规则。

开赛时间、比分和比赛状态由独立动态任务从数据库领取比赛后更新。它与赔率使用
相同的开赛阶段频率和失败退避，但每场只有一个动态任务；状态变成“完”后停止，
赔率则在开赛超过 3 小时后继续完成最终核验。

完整参数示例：

```bash
sync-match-status \
  --list-refresh-seconds 60 \
  --detail-refresh-seconds 60 \
  --detail-batch-size 10 \
  --dynamic-refresh-seconds 5 \
  --dynamic-batch-size 10 \
  --odds-refresh-seconds 5 \
  --odds-batch-size 6 \
  --odds-match-concurrency 2 \
  --odds-match-timeout-seconds 60 \
  --odds-page-concurrency 8 \
  --health-host 127.0.0.1 \
  --health-port 8080
```

显示采集用的浏览器窗口：

```bash
sync-match-status --headed
```

关闭状态页和监控端点：

```bash
sync-match-status --health-port 0
```

同一个数据库同一时间只允许一个 `sync-match-status` 进程运行，避免重复采集。

## 数据保存在哪里

主要数据表如下：

| 表名 | 内容 |
| --- | --- |
| `match_status` | 比赛采集进度和完成状态 |
| `match_basic_info` | 联赛、球队、开赛时间、比分和比赛状态 |
| `match_dynamic_schedule` | 每场比赛动态信息的下次执行时间和失败退避状态 |
| `titan007_handicap_changes` | 亚让赔率变化 |
| `titan007_1x2_changes` | 胜平负赔率变化 |
| `titan007_over_under_changes` | 进球数赔率变化 |
| `titan007_odds_fetch_status` | 各比赛、各公司的赔率核验状态 |
| `titan007_odds_market_schedule` | 每场、每家公司、每个市场的下次执行时间、连续失败次数和退避状态 |

查看当前异常比赛：

```sql
SELECT match_id, company_id, market,
       consecutive_failures, abnormal_since, last_error
FROM titan007_odds_market_schedule
WHERE is_abnormal
ORDER BY abnormal_since;
```

数据库结构由 `fetch_data/migrations/*.sql` 管理，并在程序启动时自动应用。

## 验证安装

运行测试：

```bash
python -m unittest discover -s tests -v
```

查看命令的全部参数：

```bash
sync-match-status --help
fetch-odds --help
```

更详细的任务调度、数据归属和完成规则见
[`docs/architecture/code-flow.md`](../docs/architecture/code-flow.md)。
