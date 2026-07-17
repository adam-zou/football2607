# MatchWeb

只读展示 `SimpleCrawler` 已采集的比赛详情。页面需要登录，支持按上海时区日期和
归并后的比赛状态筛选，并每 60 秒自动读取最新数据。

## 配置

复制 `.env.example` 为 `.env`，至少填写：

- `SIMPLE_CRAWLER_DATABASE_URL`：与爬虫相同的 PostgreSQL 连接地址；
- `MATCH_WEB_USERNAME`、`MATCH_WEB_PASSWORD`：网页登录账号和密码；
- `MATCH_WEB_SESSION_SECRET`：用于签名登录会话的长随机字符串。

也可以继续把数据库地址放在 `SimpleCrawler/.env`；`MatchWeb/.env` 中的同名配置
优先。

## 启动

在仓库根目录执行：

```bash
python3 MatchWeb/server.py
```

默认地址为 `http://127.0.0.1:8082/`。服务只对数据库执行只读查询。

## 列表列

比赛 ID（跳转平*三合一赔率页）、联赛、开赛时间、原始比赛状态、主队、比分、
客队、爬取状态、数据更新时间。
