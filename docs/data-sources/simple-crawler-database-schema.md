# SimpleCrawler 数据库表结构与字段说明

## 1. 文档范围

本文依据 `SimpleCrawler` 和 `MatchWeb` 当前代码中的 PostgreSQL 建表语句、数据模型、
解析器和写入逻辑整理，描述两个服务实际使用的数据库表结构与字段语义。

主要代码来源：

- `SimpleCrawler/fetch_match_ids.py`
- `SimpleCrawler/fetch_match_details.py`
- `SimpleCrawler/odds_collection.py`
- `SimpleCrawler/odds_market_state.py`
- `SimpleCrawler/check_match_completion.py`
- `SimpleCrawler/simple_crawler/models.py`
- `SimpleCrawler/simple_crawler/odds_parser.py`
- `SimpleCrawler/simple_crawler/companies.py`
- `MatchWeb/server.py`

数据库由环境变量 `SIMPLE_CRAWLER_DATABASE_URL` 指定，代码按 PostgreSQL 语法
创建和访问表。本文描述的是代码声明的目标结构；如果数据库由较早版本创建，仍应
使用 PostgreSQL 系统目录或 `psql` 的 `\d+` 命令核对线上实例是否完全一致。

## 2. 表结构总览

| 表名 | 用途 | 主键 |
| --- | --- | --- |
| `match_ids` | 保存已发现的比赛 ID 和整场比赛的爬取状态 | `match_id` |
| `match_details` | 保存比赛详情、比分和源站比赛状态 | `match_id` |
| `titan007_handicap_changes` | 保存亚让赔率变动记录 | `(match_id, company_id, seq)` |
| `titan007_1x2_changes` | 保存胜平负赔率变动记录 | `(match_id, company_id, seq)` |
| `titan007_over_under_changes` | 保存进球数赔率变动记录 | `(match_id, company_id, seq)` |
| `titan007_odds_market_state` | 保存每个比赛、公司和市场页面的最近采集状态 | `(match_id, company_id, market)` |
| `wecom_match_market_push_state` | 保存企业微信通知首次基线是否已建立 | `state_key` |
| `wecom_match_market_pushes` | 保存每个比赛市场的通知去重、快照和发送状态 | `(match_id, market_type)` |
| `match_web_pb_status` | 保存 PB 页面每场比赛的共享关注/作废状态 | `match_id` |

仓库还提供三个不存储数据的可选 PostgreSQL 视图：

| 视图名 | 用途 |
| --- | --- |
| `match_odds_filter_hits` | 展开每条满足低赔率筛选条件的历史记录 |
| `match_odds_filter_summary` | 汇总任一赔率类别至少命中三家公司的比赛 |
| `match_odds_filter_market_summary` | 按命中类别汇总最大盘口及其比分结算结果 |

表关系如下：

- `match_details.match_id` 外键关联 `match_ids.match_id`，删除比赛 ID 时级联删除详情。
- `titan007_odds_market_state.match_id` 外键关联 `match_ids.match_id`，删除比赛 ID 时
  级联删除页面采集状态。
- 三张赔率变动表的 `match_id` 与 `match_ids.match_id` 是逻辑关联，代码没有为其
  建立数据库外键。
- `wecom_match_market_pushes.match_id` 与 `match_details.match_id` 是逻辑关联，
  不声明外键，以保留已发送通知的审计记录。
- `company_id` 使用代码内固定映射，没有单独的公司维表。

## 3. 公共取值和规则

### 3.1 赔率公司

三张赔率变动表和赔率页面状态表只允许以下 `company_id`：

| `company_id` | 代码中的公司名称 |
| ---: | --- |
| `3` | `Crow*` |
| `4` | `立*` |
| `8` | `36*` |
| `24` | `12*` |
| `31` | `利*` |
| `47` | `平*` |

公司名称中的 `*` 是代码当前保存的展示文本，不应自行补全公司名称。

### 3.2 市场编码

| 存储值 | 含义 | 对应赔率变动表 |
| --- | --- | --- |
| `handicap` | 亚让 | `titan007_handicap_changes` |
| `one_x_two` | 胜平负 | `titan007_1x2_changes` |
| `over_under` | 进球数 | `titan007_over_under_changes` |

### 3.3 变动方向

所有 `*_movement` 字段只允许以下非空值：

| 存储值 | 页面颜色 | 含义 |
| --- | --- | --- |
| `上升` | `red` | 相对上一条历史报价上升 |
| `下降` | `green` | 相对上一条历史报价下降 |
| `不变` | 空颜色或无颜色 | 相对上一条历史报价不变 |

封盘时市场值和对应的变动方向均为 `NULL`。

### 3.4 时间字段

- `created_at` 和 `updated_at` 均为 PostgreSQL `TIMESTAMPTZ`，默认使用数据库
  `NOW()`。
- `updated_at` 没有数据库触发器自动维护，只在代码执行相应更新语句时刷新。
- `match_details.scheduled_time` 是源站开赛时间原文，使用 `TEXT` 保存。正常可解析
  格式为 `YYYY-MM-DD HH:MM`，业务查询将其按 `Asia/Shanghai` 时区解释。
- 赔率变动记录中的 `change_time` 也是源站原文，使用 `TEXT` 保存，不补充年份，
  不转换成 PostgreSQL 时间类型。

## 4. `match_ids`

保存从 Titan007 比赛列表发现的比赛标识，并记录该比赛是否已完成全部最终赔率
快照采集。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 约束/键 | 字段说明 |
| --- | --- | :---: | --- | --- | --- |
| `match_id` | `BIGINT` | 否 | 无 | 主键 | Titan007 比赛 ID，也是其他比赛数据的业务关联键 |
| `crawl_status` | `TEXT` | 否 | `'未完成'` | 检查约束 | SimpleCrawler 对整场比赛的爬取状态 |
| `created_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 首次发现并插入该比赛 ID 的时间 |
| `updated_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 比赛爬取状态最近一次被代码更新的时间 |

`crawl_status` 允许值：

| 状态 | 含义 |
| --- | --- |
| `未完成` | 比赛仍需普通采集或最终快照核验 |
| `已完成` | 固定 6 家公司、3 个市场共 18 个最终快照页面全部成功写入 |
| `暂停爬取` | 兼容旧版流程的状态；仍会进入历史收尾流程 |
| `异常` | 兼容旧版流程的状态；仍会进入历史收尾流程 |

新发现的相同 `match_id` 使用 `ON CONFLICT DO NOTHING`，不会重复插入，也不会仅因
再次发现而刷新 `updated_at`。当前代码只有在最终快照完成并把状态更新为
`已完成`时显式刷新该字段。

## 5. `match_details`

保存每场比赛详情页的最新快照。相同 `match_id` 再次抓取成功时更新详情字段并刷新
`updated_at`，`created_at` 保持首次成功写入时间。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 约束/键 | 字段说明 |
| --- | --- | :---: | --- | --- | --- |
| `match_id` | `BIGINT` | 否 | 无 | 主键；外键 | Titan007 比赛 ID；关联 `match_ids(match_id)`，删除主记录时级联删除 |
| `league` | `TEXT` | 否 | 无 |  | 详情页展示的联赛名称 |
| `home_team` | `TEXT` | 否 | 无 |  | 详情页展示的主队名称 |
| `away_team` | `TEXT` | 否 | 无 |  | 详情页展示的客队名称 |
| `scheduled_time` | `TEXT` | 否 | 无 |  | 详情页展示的开赛时间原文；正常格式为 `YYYY-MM-DD HH:MM`，按北京时间解释 |
| `home_score` | `SMALLINT` | 是 | `NULL` |  | 当前主队比分；未开始或无法解析时为 `NULL` |
| `away_score` | `SMALLINT` | 是 | `NULL` |  | 当前客队比分；未开始或无法解析时为 `NULL` |
| `status_text` | `TEXT` | 否 | 无 |  | 详情页原始比赛状态；页面无值时解析器写入 `未开始` |
| `created_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 首次成功保存比赛详情的时间 |
| `updated_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 最近一次成功保存比赛详情的时间 |

`status_text` 没有数据库检查约束。当前统计和调度代码识别的典型值包括：

- `未开始`；
- `上`、`中`、`下`、`加`、`点`、`进行中`以及分钟文本；
- `完`、`推迟`、`取消`、`待定`。

## 6. 赔率变动记录的共同字段

三张赔率变动表都使用 `(match_id, company_id, seq)` 作为主键，并包含以下共同
字段：

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 约束/键 | 字段说明 |
| --- | --- | :---: | --- | --- | --- |
| `match_id` | `BIGINT` | 否 | 无 | 联合主键 | Titan007 比赛 ID；与 `match_ids` 逻辑关联，但没有外键 |
| `company_id` | `INTEGER` | 否 | 无 | 联合主键；检查约束 | 赔率公司 ID，只允许 `3`、`4`、`8`、`24`、`31`、`47` |
| `seq` | `INTEGER` | 否 | 无 | 联合主键；`seq > 0` | 同一比赛、公司和市场内稳定的历史顺序号 |
| `match_minute` | `SMALLINT` | 是 | `NULL` |  | 页面显示的比赛分钟；赛前记录为 `NULL` |
| `home_score` | `SMALLINT` | 是 | `NULL` |  | 该次变动发生时的主队比分；赛前记录为 `NULL` |
| `away_score` | `SMALLINT` | 是 | `NULL` |  | 该次变动发生时的客队比分；赛前记录为 `NULL` |
| `change_time` | `TEXT` | 否 | 无 |  | 页面“变化时间”原文，不做日期时间标准化 |
| `source_status` | `TEXT` | 否 | 无 |  | 页面状态原文，例如 `滚`、`即`、`早`、`(初盘)` |
| `is_suspended` | `BOOLEAN` | 否 | 无 | 检查约束参与字段 | 是否为封盘记录；为 `TRUE` 时该市场的全部报价和变动字段必须为 `NULL` |
| `created_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 该赔率变动记录首次写入时间 |
| `updated_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 同一主键下持久化内容最近一次实际变化的时间 |

`seq` 按网页 DOM 顺序反向编号：页面最底部的记录为 `1`，向页面顶部递增。因此
`seq ASC` 表示从页面底部到顶部，`seq DESC` 表示网页从上到下的原始展示顺序。

相同主键再次写入时，只有比分、状态、市场值、变动方向或封盘状态等持久化内容
实际变化，代码才执行更新并刷新 `updated_at`；内容完全相同时不会制造无意义更新。

## 7. `titan007_handicap_changes`

保存亚让赔率变动记录。除第 6 节的共同字段外，还包含以下字段：

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 约束 | 字段说明 |
| --- | --- | :---: | --- | --- | --- |
| `home_odds` | `NUMERIC(8,3)` | 是 | `NULL` | 封盘时必须为 `NULL` | 主队赔率 |
| `home_odds_movement` | `TEXT` | 是 | `NULL` | `上升`、`下降`、`不变` | 主队赔率的变动方向 |
| `handicap_raw` | `TEXT` | 是 | `NULL` | 封盘时必须为 `NULL` | 亚让盘口页面原文，例如 `半球`、`半球/一球` |
| `handicap_value` | `NUMERIC(6,2)` | 是 | `NULL` | 封盘时必须为 `NULL` | 盘口换算值；受让盘口使用负数，无法识别时为 `NULL` |
| `handicap_movement` | `TEXT` | 是 | `NULL` | `上升`、`下降`、`不变` | 亚让盘口的变动方向 |
| `away_odds` | `NUMERIC(8,3)` | 是 | `NULL` | 封盘时必须为 `NULL` | 客队赔率 |
| `away_odds_movement` | `TEXT` | 是 | `NULL` | `上升`、`下降`、`不变` | 客队赔率的变动方向 |

盘口换算示例：`平手` → `0.00`，`平手/半球` → `0.25`，`半球` → `0.50`，
`半球/一球` → `0.75`，`受让平手/半球` → `-0.25`。无法识别时保留
`handicap_raw`，并将 `handicap_value` 置为 `NULL`。

## 8. `titan007_1x2_changes`

保存胜平负赔率变动记录。除第 6 节的共同字段外，还包含以下字段：

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 约束 | 字段说明 |
| --- | --- | :---: | --- | --- | --- |
| `home_win_odds` | `NUMERIC(8,3)` | 是 | `NULL` | 封盘时必须为 `NULL` | 主胜赔率 |
| `home_win_odds_movement` | `TEXT` | 是 | `NULL` | `上升`、`下降`、`不变` | 主胜赔率的变动方向 |
| `draw_odds` | `NUMERIC(8,3)` | 是 | `NULL` | 封盘时必须为 `NULL` | 和局赔率 |
| `draw_odds_movement` | `TEXT` | 是 | `NULL` | `上升`、`下降`、`不变` | 和局赔率的变动方向 |
| `away_win_odds` | `NUMERIC(8,3)` | 是 | `NULL` | 封盘时必须为 `NULL` | 客胜赔率 |
| `away_win_odds_movement` | `TEXT` | 是 | `NULL` | `上升`、`下降`、`不变` | 客胜赔率的变动方向 |

## 9. `titan007_over_under_changes`

保存进球数赔率变动记录。除第 6 节的共同字段外，还包含以下字段：

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 约束 | 字段说明 |
| --- | --- | :---: | --- | --- | --- |
| `over_odds` | `NUMERIC(8,3)` | 是 | `NULL` | 封盘时必须为 `NULL` | 大球赔率 |
| `over_odds_movement` | `TEXT` | 是 | `NULL` | `上升`、`下降`、`不变` | 大球赔率的变动方向 |
| `total_line_raw` | `TEXT` | 是 | `NULL` | 封盘时必须为 `NULL` | 进球数盘口页面原文，例如 `2/2.5` |
| `total_line_value` | `NUMERIC(6,2)` | 是 | `NULL` | 封盘时必须为 `NULL` | 盘口换算后的数值；无法识别时为 `NULL` |
| `total_line_movement` | `TEXT` | 是 | `NULL` | `上升`、`下降`、`不变` | 进球数盘口的变动方向 |
| `under_odds` | `NUMERIC(8,3)` | 是 | `NULL` | 封盘时必须为 `NULL` | 小球赔率 |
| `under_odds_movement` | `TEXT` | 是 | `NULL` | `上升`、`下降`、`不变` | 小球赔率的变动方向 |

单一盘口直接转成数值；分段盘口取两个端点的算术平均值。例如 `2.5` → `2.50`、
`2/2.5` → `2.25`。无法识别时保留 `total_line_raw`，并将
`total_line_value` 置为 `NULL`。

## 10. `titan007_odds_market_state`

保存每个“比赛 × 公司 × 市场”页面的最新采集结果和最终快照进度。该表不重复保存
赔率值；完整赔率变动记录保存在对应的三张市场表中。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 约束/键 | 字段说明 |
| --- | --- | :---: | --- | --- | --- |
| `match_id` | `BIGINT` | 否 | 无 | 联合主键；外键 | Titan007 比赛 ID；关联 `match_ids(match_id)`，删除主记录时级联删除 |
| `company_id` | `INTEGER` | 否 | 无 | 联合主键；检查约束 | 赔率公司 ID，只允许 `3`、`4`、`8`、`24`、`31`、`47` |
| `market` | `TEXT` | 否 | 无 | 联合主键；检查约束 | 市场编码，只允许 `handicap`、`one_x_two`、`over_under` |
| `last_attempt_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 最近一次尝试采集该页面的时间 |
| `last_success_at` | `TIMESTAMPTZ` | 是 | `NULL` | 成功状态检查约束参与字段 | 最近一次成功解析并保存页面的时间；尚未成功时为 `NULL` |
| `fetch_status` | `TEXT` | 否 | 无 | 检查约束 | 最近一次采集状态，只允许 `待抓取`、`成功`、`失败` |
| `row_count` | `INTEGER` | 是 | `NULL` | `row_count >= 0` | 最近一次成功解析出的赔率变动记录数，合法空页面为 `0` |
| `content_hash` | `CHAR(64)` | 是 | `NULL` | 64 位小写十六进制检查 | 最近一次成功解析结果的 SHA-256 摘要 |
| `last_error` | `TEXT` | 是 | `NULL` | 成功时必须为 `NULL` | 最近一次失败原因；最近一次成功后清空 |
| `final_required` | `BOOLEAN` | 否 | `FALSE` |  | 是否仍要求执行最终快照采集 |
| `final_success_at` | `TIMESTAMPTZ` | 是 | `NULL` |  | 该页面最终快照成功写入时间；尚未完成时为 `NULL` |
| `created_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 页面状态记录首次创建时间 |
| `updated_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 页面状态最近一次更新的时间 |

当 `fetch_status = '成功'` 时，数据库检查约束要求：

- `last_success_at`、`row_count`、`content_hash` 均非空；
- `last_error` 必须为 `NULL`。

普通采集成功会更新成功时间、记录数、摘要并清空错误。普通采集失败只更新尝试时间、
状态和错误，保留上一次成功的时间、记录数与摘要。最终快照失败会设置
`final_required = TRUE` 并清空 `final_success_at`；最终快照成功会设置
`final_required = FALSE` 并写入 `final_success_at`。

### 10.1 `match_web_pb_status`

该表由 `MatchWeb/server.py` 在服务启动时以 `CREATE TABLE IF NOT EXISTS` 创建并拥有，
不属于爬虫采集状态。PB 页面上的“关注”和“作废”操作以比赛为单位共享保存；同一
比赛只能有一个当前状态。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 约束/键 | 字段说明 |
| --- | --- | :---: | --- | --- | --- |
| `match_id` | `BIGINT` | 否 | 无 | 主键 | Titan007 比赛 ID；写入前由应用确认 `match_details` 中存在该比赛 |
| `status` | `TEXT` | 否 | 无 | 检查约束 | 只允许 `关注` 或 `作废` |
| `updated_by` | `TEXT` | 否 | 无 |  | 最近设置该状态的登录用户名 |
| `updated_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 状态最近更新时间 |

再次设置同一比赛时使用 `ON CONFLICT (match_id) DO UPDATE` 覆盖状态、操作者和更新时间。
该表不声明外键，以免展示侧状态改变爬虫表的删除和生命周期语义。

### 10.2 企业微信通知状态表

`SimpleCrawler/push_wecom_matches.py` 以 `CREATE TABLE IF NOT EXISTS` 创建并拥有
以下两张表。

`wecom_match_market_push_state` 是单例初始化标记：

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 约束/键 | 字段说明 |
| --- | --- | :---: | --- | --- | --- |
| `state_key` | `TEXT` | 否 | 无 | 主键；固定为 `match_market_baseline` | 基线类型 |
| `initialized_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 首次基线建立时间 |

独立初始化表保证即使首轮没有符合条件的比赛，后续新比赛也不会被
错当成基线而漏发。

`wecom_match_market_pushes` 每个“比赛 + 市场类别”一行：

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 约束/键 | 字段说明 |
| --- | --- | :---: | --- | --- | --- |
| `match_id` | `BIGINT` | 否 | 无 | 联合主键 | Titan007 比赛 ID |
| `market_type` | `TEXT` | 否 | 无 | 联合主键；检查约束 | `over_under`、`handicap_home` 或 `handicap_away` |
| `push_status` | `TEXT` | 否 | 无 | 检查约束 | `baseline`、`pending`、`sent`、`failed` 或 `expired` |
| `company_count` | `BIGINT` | 否 | 无 | `company_count >= 3` | 发现时的命中公司数快照 |
| `line_value` | `NUMERIC(6,2)` | 是 | `NULL` |  | 发现时的最大盘口快照 |
| `league` | `TEXT` | 是 | `NULL` |  | 发现时的联赛快照 |
| `scheduled_time` | `TEXT` | 否 | 无 |  | 发现时的开赛时间快照 |
| `home_team` | `TEXT` | 否 | 无 |  | 发现时的主队快照 |
| `away_team` | `TEXT` | 否 | 无 |  | 发现时的客队快照 |
| `detected_at` | `TIMESTAMPTZ` | 否 | `NOW()` |  | 首次发现时间 |
| `last_attempt_at` | `TIMESTAMPTZ` | 是 | `NULL` |  | 最近一次 Webhook 尝试时间 |
| `sent_at` | `TIMESTAMPTZ` | 是 | `NULL` |  | Webhook 明确确认成功的时间 |
| `attempt_count` | `INTEGER` | 否 | `0` | `attempt_count >= 0` | Webhook 尝试次数 |
| `last_error` | `TEXT` | 是 | `NULL` |  | 最近一次失败摘要，不含 Webhook URL |

首次运行将当前符合条件且“未开始”的记录写为 `baseline`。之后候选
记录还必须满足 `match_ids.created_at > initialized_at`，以排除基线前已发现、
但之后才补齐赔率数据的旧比赛。新记录使用
`INSERT ... ON CONFLICT DO NOTHING` 写为 `pending`，联合主键防止
正常轮询重复推送。失败记录下轮重试；重试前比赛已开始则改为
`expired`。

## 11. 索引

除各表主键自动建立的唯一 B-tree 索引外，代码还创建以下部分索引：

```sql
CREATE INDEX IF NOT EXISTS titan007_odds_market_state_final_pending_idx
    ON titan007_odds_market_state (match_id, company_id, market)
    WHERE final_required AND final_success_at IS NULL;
```

该索引用于快速定位仍需完成最终快照的页面状态记录。

## 12. 可选赔率筛选视图

`SimpleCrawler/sql/create_odds_filter_views.sql` 创建三个实时视图。该脚本需要由
操作人员通过 `psql` 手工执行，不属于爬虫建表或
调度器启动流程；MatchWeb 当前查询也不依赖这三个视图。脚本可以重复执行，视图
每次查询都读取三张赔率变动表的当前数据，不保存独立快照。

`match_odds_filter_hits` 先要求比赛在三种市场的任意一张表中存在公司 3 数据，
再展开以下非“滚”且公司 ID 不为 4 的命中记录：

- 亚让主队赔率低于 `0.700`；
- 亚让客队赔率低于 `0.700`；
- 进球数大球赔率低于 `0.700`。

同一条亚让记录两侧同时命中时会产生两行。视图输出比赛、公司、市场、变化时间、
盘口原文与数值、命中赔率及 `home`、`away` 或 `over` 方向。

`match_odds_filter_summary` 按比赛分别计算大球、亚让主队和亚让客队的去重命中
公司数，只要任一类别达到三家公司即保留该比赛。脚本会先删除再创建此派生视图，
以允许旧版本的列顺序和结构被替换。

`match_odds_filter_market_summary` 每场比赛、每个达到三家公司的类别输出一行。
类别为 `over_under`、`handicap_home` 或 `handicap_away`，`line_value` 取该类别命中
记录中的最大盘口值。视图左连接 `match_details` 读取比分，并按亚洲盘差值返回
`全赢`、`赢半`、`走水`、`输半` 或 `全输`；比分或盘口缺失时结果为 `NULL`。

## 13. 数据完整性注意事项

1. 三张赔率变动表没有到 `match_ids` 的外键。删除 `match_ids` 记录时，数据库会
   级联删除比赛详情和赔率页面状态，但不会自动删除对应赔率变动记录。
2. 三张赔率变动表的封盘检查约束只规定“封盘时市场字段必须全部为空”，没有规定
   “非封盘时市场字段必须全部非空”。解析失败或源站缺值仍可能产生部分空字段。
3. `scheduled_time`、`change_time` 和 `status_text` 都保留源站文本。使用这些字段
   做日期或状态统计时，应沿用代码中的格式校验和状态分类规则。
4. 建表使用 `CREATE TABLE IF NOT EXISTS`，不会自动修正已经存在表的所有字段差异。
   当前代码仅对 `match_ids.crawl_status` 的部分旧结构和状态约束执行显式升级；
   已符合当前定义的状态约束不会在每轮核验时重复删除和重建。
5. `NUMERIC(8,3)` 最多保留 3 位小数；`NUMERIC(6,2)` 最多保留 2 位小数。读取时
   应保留定点数语义，避免不必要地转换为二进制浮点数。
