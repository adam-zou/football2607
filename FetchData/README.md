# FetchData

FetchData 是一个足球比赛与赔率历史采集服务。它从 Titan007 持续获取比赛，
并把数据保存到 PostgreSQL，适合用于赔率分析、比赛回溯和后续的数据建模。

## 它会做什么

运行后，服务会自动执行三类任务：

- 发现比赛，持续更新开赛时间、比赛状态和比分；
- 获取联赛、主队、客队等比赛详情；
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
共请求 18 个页面，并把结果直接写入 PostgreSQL。

调试时可以只抓一家机构，或显示浏览器窗口：

```bash
fetch-odds 3020831 --company-id 3
fetch-odds 3020831 --company-id 3 --headed
```

## 常用启动参数

默认配置通常无需修改。如需调整采集频率：

```bash
sync-match-status \
  --list-refresh-seconds 60 \
  --detail-refresh-seconds 60 \
  --detail-batch-size 10 \
  --odds-refresh-seconds 5 \
  --odds-batch-size 1
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
| `titan007_handicap_changes` | 亚让赔率变化 |
| `titan007_1x2_changes` | 胜平负赔率变化 |
| `titan007_over_under_changes` | 进球数赔率变化 |
| `titan007_odds_fetch_status` | 各比赛、各公司的赔率核验状态 |

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
