# Titan007 赔率变动数据存储需求

## 范围

本文定义 Titan007 以下三个赔率变化页面的数据库存储结构：

- 亚让：`changeDetail/handicap.aspx`
- 胜平负：`changeDetail/1x2.aspx`
- 进球数：`changeDetail/overunder.aspx`

三个市场分别存入三张数据库表，不合并为一张通用赔率表：

- `titan007_handicap_changes`
- `titan007_1x2_changes`
- `titan007_over_under_changes`

另使用 `titan007_odds_market_state` 记录每个比赛、公司和市场页面最近一次采集
状态、记录数和内容摘要，不重复保存赔率值。

本文先使用逻辑类型描述字段，实际 PostgreSQL 映射见“PostgreSQL 实现”。

## 采集机构范围

每场比赛必须分别采集以下 6 家公司的数据：

| `company_id` | 公司名称 |
| ---: | --- |
| 3 | Crow* |
| 4 | 立* |
| 8 | 36* |
| 24 | 12* |
| 31 | 利* |
| 47 | 平* |

URL 查询参数名固定为小写 `companyid`。对同一个 `match_id`，只需替换该参数即可获取不同公司的页面数据：

```text
https://vip.titan007.com/changeDetail/handicap.aspx?id={match_id}&companyid={company_id}&l=0
https://vip.titan007.com/changeDetail/1x2.aspx?id={match_id}&companyid={company_id}&l=0
https://vip.titan007.com/changeDetail/overunder.aspx?id={match_id}&companyid={company_id}&l=0
```

因此每场比赛的完整采集范围是 `6 家公司 × 3 个市场 = 18 个页面`。每个页面解析出的记录必须写入对应市场表，并保存实际请求使用的 `company_id`；不得将某一家公司的 ID 写死为默认值。

公司名称按照上表作为配置和展示映射，记录关联以 Titan007 的 `company_id` 为准。名称中的 `*` 按当前已确认文本原样保留，不自行补全或猜测公司全名。

## 三张表的共同规则

### 来源标识

每条记录至少包含：

| 字段 | 逻辑类型 | 可空 | 含义 |
| --- | --- | --- | --- |
| `match_id` | string / integer | 否 | URL 参数 `id`，Titan007 比赛 ID |
| `company_id` | string / integer | 否 | URL 参数 `companyid`，赔率机构 ID |
| `seq` | positive integer | 否 | 同一比赛、公司和市场内的网页行顺序；页面最底部的数据行为 1，向页面顶部递增 |

`seq` 必须保存，因为网页可能出现“比赛时间”和“变化时间”都相同的多条记录。网页已经给出了权威行顺序，解析器不得根据比赛时间、变化时间、状态或赔率值重新排序。`seq ASC` 表示从网页底部到顶部；`seq DESC` 表示网页原始的从上到下显示顺序。

解析含 `N` 条数据的页面时，直接使用网页已有的 DOM 行位置。DOM 中第 `dom_position` 条数据的序号为：

```text
seq = N - dom_position + 1
```

也就是从页面底部向顶部编号：最底部数据行为 `seq = 1`，最顶部数据行为 `seq = N`。这个计算只反转网页行位置，不解析或比较任何时间字段。页面顶部增加新的赔率变动记录时，已有赔率变动记录的 `seq` 不会整体改变。该稳定性依赖页面继续保留完整的赔率变动记录；如果网站会删除底部记录，则仍需额外的抓取批次或稳定记录标识处理增量去重。

### 比赛状态字段

| 字段 | 逻辑类型 | 可空 | 含义 |
| --- | --- | --- | --- |
| `match_minute` | integer | 是 | 页面“时间”；赛前记录为 `null` |
| `home_score` | integer | 是 | 从页面比分左侧解析；赛前为 `null` |
| `away_score` | integer | 是 | 从页面比分右侧解析；赛前为 `null` |
| `change_time` | string | 否 | 页面“变化时间”的原始显示文本，不做日期时间标准化 |
| `source_status` | string | 否 | 页面原始状态，例如 `滚`、`即`、`早`、`(初盘)` |
| `is_suspended` | boolean | 否 | 页面中间三个市场单元格显示“封”时为 `true` |

数据库不保存组合比分字符串。页面 `1-1` 必须拆成 `home_score = 1`、`away_score = 1`。

`change_time` 按页面文本原样保存，例如 `7-13 22:21` 或 `07-13 21:57`。保留页面是否补零的差异，不补充年份，也不转换为数据库日期时间；后续是否增加标准化时间字段另行决定。

### 空值规则

- 赛前记录没有比赛时间和比分，因此 `match_minute`、`home_score`、`away_score` 均为 `null`。
- 封盘记录的三个市场值不存在，对应原始值、数值转换值和变动方向均为 `null`。
- 不使用空字符串、`0` 或特殊赔率值代替缺失值。
- `0-0` 是有效比分，必须保存为两个数值 `0`，不能被当成缺失值。

### 变动方向

网页颜色必须转换为业务值保存，不能只保存原始颜色字符串：

| 页面颜色 | 存储值 |
| --- | --- |
| `red` | `上升` |
| `green` | `下降` |
| 空颜色或没有颜色 | `不变` |

颜色属于单个赔率或盘口，不属于整行。因此三个市场值分别拥有自己的变动方向字段。

变动方向字段的允许值为 `上升`、`下降`、`不变`；当对应市场值为 `null` 时，变动方向也为 `null`。

本文表格中的 `movement` 是领域枚举，不是数据库内置类型。逻辑定义为：

```text
movement = "上升" | "下降" | "不变" | null
```

在尚未确定数据库实现前，各 `*_movement` 字段按受限字符串理解。落库时可使用字符串字段加检查约束，或使用数据库枚举类型，但存储值必须保持为上述三个中文值。

## 亚让表

表名：`titan007_handicap_changes`

除共同字段外，保存：

| 字段 | 逻辑类型 | 可空 | 含义 |
| --- | --- | --- | --- |
| `home_odds` | decimal | 是 | 主队赔率；封盘时为 `null` |
| `home_odds_movement` | movement enum | 是 | 主队赔率变动方向 |
| `handicap_raw` | string | 是 | 页面盘口原文，例如 `半球`、`半球/一球` |
| `handicap_value` | decimal | 是 | 盘口转换后的计算值，例如 `0.5`、`0.75`、`-0.25` |
| `handicap_movement` | movement enum | 是 | 盘口变动方向 |
| `away_odds` | decimal | 是 | 客队赔率；封盘时为 `null` |
| `away_odds_movement` | movement enum | 是 | 客队赔率变动方向 |

表头中的球队名称只是当前比赛的展示文本，不能用于字段命名。固定字段必须使用 `home_odds` 和 `away_odds`。

盘口转换规则：

- 必须同时保存 `handicap_raw` 和 `handicap_value`。
- 平手为 `0`，平手/半球为 `0.25`，半球为 `0.5`，半球/一球为 `0.75`，一球为 `1.0`，依此类推。
- 受让盘口使用负数，例如受让平手/半球为 `-0.25`。
- 无法识别的盘口原文不得猜测数值；保留原文，并将 `handicap_value` 设为 `null`，交由异常处理流程记录。

## 胜平负表

表名：`titan007_1x2_changes`

除共同字段外，保存：

| 字段 | 逻辑类型 | 可空 | 含义 |
| --- | --- | --- | --- |
| `home_win_odds` | decimal | 是 | 主胜赔率；封盘时为 `null` |
| `home_win_odds_movement` | movement enum | 是 | 主胜赔率变动方向 |
| `draw_odds` | decimal | 是 | 和局赔率；封盘时为 `null` |
| `draw_odds_movement` | movement enum | 是 | 和局赔率变动方向 |
| `away_win_odds` | decimal | 是 | 客胜赔率；封盘时为 `null` |
| `away_win_odds_movement` | movement enum | 是 | 客胜赔率变动方向 |

三个固定业务字段是主胜赔率、和局赔率和客胜赔率。页面表头中的球队名称不能用于数据库字段命名。

## 进球数表

表名：`titan007_over_under_changes`

除共同字段外，保存：

| 字段 | 逻辑类型 | 可空 | 含义 |
| --- | --- | --- | --- |
| `over_odds` | decimal | 是 | 大球赔率；封盘时为 `null` |
| `over_odds_movement` | movement enum | 是 | 大球赔率变动方向 |
| `total_line_raw` | string | 是 | 页面进球数盘口原文，例如 `2/2.5` |
| `total_line_value` | decimal | 是 | 盘口转换后的计算值，例如 `0.5`、`0.75`、`1`、`1.25` |
| `total_line_movement` | movement enum | 是 | 进球数盘口变动方向 |
| `under_odds` | decimal | 是 | 小球赔率；封盘时为 `null` |
| `under_odds_movement` | movement enum | 是 | 小球赔率变动方向 |

进球数列就是该市场的盘口。必须同时保存页面原文和数值转换结果：

- 整数或单一小数盘口直接转换，例如 `1` → `1.0`、`2.5` → `2.5`。
- 分段盘口取两个端点的算术平均值，例如 `0.5/1` → `0.75`、`1/1.5` → `1.25`、`2/2.5` → `2.25`。
- 无法识别的盘口原文不得猜测数值；保留原文，并将 `total_line_value` 设为 `null`，交由异常处理流程记录。

## DOM 解析约束

- 亚让和进球数主表位于 `#odds2 table`。
- 胜平负主表位于 `#odds table`。
- 跳过第一行表头，保持其余数据行的 DOM 顺序不变。不得按任何时间字段排序；从最底部数据行开始分配 `seq = 1`，向页面顶部递增。
- 普通行有 7 个逻辑字段。
- 封盘行中间是一个 `colspan="3"` 的“封”单元格，物理上只有 5 个 `td`；解析器必须展开为三个 `null` 市场值。
- 赛前行前两个单元格为空，应解析为 `match_minute = null`、`home_score = null`、`away_score = null`。
- 解析颜色时读取各市场单元格内部的 `font[color]`；空 `color` 属性和缺失颜色均映射为 `不变`。

## PostgreSQL 实现

三张表由 `SimpleCrawler/fetch_odds_pages.py` 中的建表语句创建，实际类型规则如下：

- `match_id` 使用 `BIGINT`，`company_id` 和 `seq` 使用 `INTEGER`。
- 比赛分钟和比分使用可空 `SMALLINT`。
- `change_time` 和 `source_status` 使用 `TEXT`；`change_time` 原样保存页面文本。
- 赔率使用 `NUMERIC(8, 3)`，转换后的盘口使用 `NUMERIC(6, 2)`。
- movement 使用可空 `TEXT`，并通过检查约束限制为 `上升`、`下降`、`不变`。
- 三张表分别以 `(match_id, company_id, seq)` 为主键。
- 封盘检查约束保证三个市场值、转换值和 movement 均为 `null`。

`SimpleCrawler/fetch_odds_pages.py` 以“机构 × 市场”页面作为最小采集和事务单元。
成功页面的赔率变动记录按最多 500 行一批写入，并在同一事务更新页面采集状态；
失败页面不写赔率数据，只单独提交失败状态。因此单个进球数页面失败不会丢弃
同一轮中已经成功保存的亚让或胜平负页面。主键冲突时只有页面字段实际变化才
更新记录并刷新 `updated_at`，未变化的历史记录保留原更新时间。

采集器从服务器返回的主文档 HTML 解析数据，并验证 HTTP 状态、错误或拦截页
关键字、市场容器及市场导航。只有确认是赔率市场页面但没有目标表格时才视为
合法空市场；空市场不执行 INSERT，也不删除数据库已有记录。

三张赔率表不设置到 `match_ids` 的外键；页面状态表通过 `match_id` 关联
`match_ids`。正常选择和显式 ID 模式都要求比赛处于启用的采集状态、已有
`match_details`，且可解析的北京时间位于当前时间前 4 小时至后
30 分钟。普通队列优先处理进行中、即将开赛和最近完场比赛。

## 赔率页面采集状态

`titan007_odds_market_state` 以 `(match_id, company_id, market)` 为主键，包含：

- `last_attempt_at`：最近一次页面采集时间；
- `last_success_at`：最近一次成功时间；
- `fetch_status`：`待抓取`、`成功` 或 `失败`；
- `row_count`：最近一次成功解析的赔率变动记录数；
- `content_hash`：规范化解析结果的 SHA-256 摘要；
- `last_error`：最近一次失败原因；
- `final_required`、`final_success_at`：最终快照是否待处理及成功时间；
- `created_at`、`updated_at`：状态行的创建和更新时间。

成功会刷新记录数、摘要和成功时间并清空错误；失败会保留上一次成功的记录数、
摘要和成功时间，只更新尝试时间、状态和错误。

三张赔率变化表同样包含 `created_at` 和 `updated_at`：首次写入时两者由数据库
赋值；相同 `(match_id, company_id, seq)` 再次写入时保留 `created_at` 并刷新
`updated_at`。

最终快照由 `SimpleCrawler/check_match_completion.py` 单独执行。北京时间开赛至少
四小时后，它固定采集六家公司三个市场的 18 个页面。每个成功页面都在
同一事务中写入赔率变动记录并设置 `final_success_at`；失败页面保持
`final_required = true`。同一公司的待处理市场共用一个 Chromium context，
但仍各自维护状态；重试批次只包含失败市场。只有全部 18 页成功写入后才把
`crawl_status` 更新为 `已完成`，不再额外访问相同页面比较记录数。
