# MatchWeb

只读展示 `SimpleCrawler` 已采集的比赛详情。页面需要登录，支持按上海时区日期和
归并后的比赛状态筛选，并每 60 秒自动读取最新数据。“启用筛选”默认勾选，要求
非“滚”记录中，让球盘两侧任一赔率或大小球盘的大球赔率曾低于 `0.7`，并且
公司 3 在三个赔率市场的任意一个市场中存在数据。

## 配置

复制 `.env.example` 为 `.env`，填写：

- `SIMPLE_CRAWLER_DATABASE_URL`：与爬虫相同的 PostgreSQL 连接地址；
- `MATCH_WEB_SESSION_SECRET`：用于签名登录会话的长随机字符串。
- `MATCH_WEB_USERS_FILE`：可选的账号文件位置，默认是 `MatchWeb/users.json`。

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

## 启动

在仓库根目录执行：

```bash
python3 MatchWeb/server.py
```

默认地址为 `http://127.0.0.1:8082/`。服务只对数据库执行只读查询。

## 列表列

比赛 ID（跳转 Nowscore 公司 3 三合一赔率页）、联赛、开赛时间、原始比赛状态、主队、比分、
客队、筛选标记。命中比赛显示“详情”，悬停或键盘聚焦后显示命中记录的公司名称和
`change_time`。
