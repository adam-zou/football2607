-- 查询当前仍未恢复的赔率页面。
-- 页面成功后 consecutive_failures 会清零，因此这里只显示当前失败页面。
SELECT
    schedule.match_id AS 比赛_id,
    schedule.company_id AS 公司_id,
    CASE schedule.market
        WHEN 'handicap' THEN '亚让'
        WHEN 'one_x_two' THEN '胜平负'
        WHEN 'over_under' THEN '进球数'
        ELSE schedule.market
    END AS 市场,
    basic.league AS 联赛,
    basic.home_team AS 主队,
    basic.away_team AS 客队,
    basic.scheduled_time AS 开赛时间,
    schedule.consecutive_failures AS 连续失败次数,
    schedule.last_attempt_at AS 最后尝试时间,
    schedule.next_attempt_at AS 下次尝试时间,
    schedule.is_abnormal AS 是否异常,
    schedule.abnormal_since AS 异常开始时间,
    CASE
        WHEN schedule.last_error ILIKE '%ERR_TUNNEL_CONNECTION_FAILED%'
            THEN '代理隧道失败'
        WHEN schedule.last_error ILIKE '%Timeout%'
          OR schedule.last_error ILIKE '%超时%'
          OR schedule.last_error ILIKE '%exceeded%'
            THEN '页面或任务超时'
        WHEN schedule.last_error ILIKE '%row shape%'
          OR schedule.last_error ILIKE '%cells%'
            THEN '页面列数不兼容'
        WHEN schedule.last_error ILIKE '%验证码%'
          OR schedule.last_error ILIKE '%Access Denied%'
          OR schedule.last_error ILIKE '%WAF%'
          OR schedule.last_error ILIKE '%拦截%'
            THEN '页面被拦截'
        WHEN schedule.last_error ILIKE '%HTTP%'
            THEN 'HTTP错误'
        WHEN schedule.last_error ILIKE '%市场结构%'
            THEN '缺少市场结构'
        WHEN schedule.last_error ILIKE '%代理验证失败%'
            THEN '代理验证失败'
        ELSE '其他错误'
    END AS 失败分类,
    schedule.last_error AS 详细错误,
    CONCAT(
        'https://vip.titan007.com/changeDetail/',
        CASE schedule.market
            WHEN 'handicap' THEN 'handicap.aspx'
            WHEN 'one_x_two' THEN '1x2.aspx'
            WHEN 'over_under' THEN 'overunder.aspx'
        END,
        '?id=', schedule.match_id,
        '&companyid=', schedule.company_id,
        '&l=0'
    ) AS 页面_url
FROM titan007_odds_market_schedule AS schedule
LEFT JOIN match_basic_info AS basic
    ON basic.match_id = schedule.match_id
WHERE schedule.consecutive_failures > 0
ORDER BY
    schedule.is_abnormal DESC,
    schedule.consecutive_failures DESC,
    schedule.last_attempt_at DESC,
    schedule.match_id,
    schedule.company_id,
    schedule.market;


-- 只查询可能需要调整页面判定或解析规则的失败页面。
SELECT
    match_id,
    company_id,
    market,
    consecutive_failures,
    last_attempt_at,
    last_error,
    CONCAT(
        'https://vip.titan007.com/changeDetail/',
        CASE market
            WHEN 'handicap' THEN 'handicap.aspx'
            WHEN 'one_x_two' THEN '1x2.aspx'
            WHEN 'over_under' THEN 'overunder.aspx'
        END,
        '?id=', match_id,
        '&companyid=', company_id,
        '&l=0'
    ) AS 页面_url
FROM titan007_odds_market_schedule
WHERE consecutive_failures > 0
  AND (
      last_error ILIKE '%row shape%'
      OR last_error ILIKE '%cells%'
      OR last_error ILIKE '%市场结构%'
      OR last_error ILIKE '%invalid%'
  )
ORDER BY last_attempt_at DESC;
