# MatchWeb

只读展示 `SimpleCrawler` 已采集的比赛详情。页面需要登录，支持按上海时区比赛日和
一个或多个归并后的比赛状态筛选，默认同时展示“未开始”和“进行中”，并每 60 秒
自动读取最新数据。所选比赛日的时间窗口从前一天 21:00（含）开始，到所选日期
次日 00:00（不含）结束。“启用筛选”默认勾选，要求
排除公司 4 后的非“滚”记录中，让球盘两侧任一赔率或大小球盘的大球赔率曾低于 `0.7`，并且
公司 3 在三个赔率市场的任意一个市场中存在数据。

## 配置

复制 `.env.example` 为 `.env`，填写：

- `SIMPLE_CRAWLER_DATABASE_URL`：与爬虫相同的 PostgreSQL 连接地址；
- `MATCH_WEB_SESSION_SECRET`：用于签名登录会话的长随机字符串。
- `MATCH_WEB_USERS_FILE`：可选的账号文件位置，默认是 `MatchWeb/users.json`。
- `MATCH_WEB_MONITOR_URL`：采集监控的内部地址，默认是 `http://127.0.0.1:8081`。

也可以继续把数据库地址放在 `SimpleCrawler/.env`；`MatchWeb/.env` 中的同名配置
优先。

## 管理账号

新增账号或重设已有账号的密码：

```bash
python3 MatchWeb/manage_users.py add adam
```

命令会要求输入两次密码，密码至少 8 个字符。重复执行即可新增多个账号；账号
文件仅保存加盐密码哈希，不保存明文密码。修改账号后请重启网页服务。

查看或删除账号：

```bash
python3 MatchWeb/manage_users.py list
python3 MatchWeb/manage_users.py remove adam
```

使用 `admin` 账号登录后，也可以从页面右上角进入“用户管理”，在网页中新增用户、
重置密码或删除普通用户。用户管理页面及其接口仅允许用户名恰好为 `admin` 的已登录
账号访问，并且不允许在网页中删除 `admin` 自身。修改立即生效，无需重启服务。

同一管理员导航还提供“采集监控”入口。`/monitor/` 和
`/monitor/api/status` 由 MatchWeb 在验证管理员会话后转发到内部监控地址，浏览器
不需要直接访问 `8081`。普通账号不能访问监控页面或状态接口。

## 启动

Windows 用户如需同时启动采集调度器和本网页应用，可直接双击仓库根目录的
`start_all.bat`。两个服务会在独立命令行窗口中运行。

只启动网页应用时，在仓库根目录执行：

```bash
python3 MatchWeb/server.py
```

默认地址为 `http://127.0.0.1:8082/`；管理员可从
`http://127.0.0.1:8082/monitor/` 查看采集监控。比赛查询只读取数据库，监控路由
只读取调度器的进程内状态。

## 列表列

比赛 ID（跳转 Nowscore 公司 3 三合一赔率页）、联赛、开赛时间、原始比赛状态、主队、比分、
客队、筛选标记。命中比赛显示“详情”，悬停或键盘聚焦后显示命中记录的公司名称和
`change_time`。
