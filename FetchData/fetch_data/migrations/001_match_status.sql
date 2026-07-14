CREATE TABLE IF NOT EXISTS match_status (
    match_id BIGINT PRIMARY KEY,
    crawl_status TEXT NOT NULL DEFAULT '未完成'
        CHECK (crawl_status IN ('未完成', '已完成')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE match_status
ADD COLUMN IF NOT EXISTS crawl_status TEXT NOT NULL DEFAULT '未完成'
    CHECK (crawl_status IN ('未完成', '已完成'));

ALTER TABLE match_status
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE match_status DROP COLUMN IF EXISTS status_text;
ALTER TABLE match_status DROP COLUMN IF EXISTS status;

CREATE INDEX IF NOT EXISTS idx_match_status_crawl_status
ON match_status(crawl_status);
