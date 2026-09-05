-- 帖子索引库。DDL 幂等，每次启动都跑一遍。
--
-- 设计取舍：
--   tags / index_tags 是查询维度 -> 独立关联表 + 索引，支持组合筛选和标签云。
--   aliases / staff / links / passwords / extra 是载荷 -> JSON 列，不参与筛选。
--   raw_text 永久保留，解析器改版后可以整库重跑而不必重新导出。

CREATE TABLE IF NOT EXISTS posts (
    channel_id       INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,
    posted_at        TEXT    NOT NULL,
    edited_at        TEXT,

    title_cn         TEXT    NOT NULL DEFAULT '',
    title_en         TEXT    NOT NULL DEFAULT '',
    search_blob      TEXT    NOT NULL DEFAULT '',   -- 中文名+英文名+别名，LIKE 预筛用

    episodes         TEXT    NOT NULL DEFAULT '',
    air_date         TEXT    NOT NULL DEFAULT '',
    air_weekday      TEXT    NOT NULL DEFAULT '',
    duration         TEXT    NOT NULL DEFAULT '',
    score            REAL,
    score_text       TEXT    NOT NULL DEFAULT '',
    summary          TEXT    NOT NULL DEFAULT '',

    aliases_json     TEXT    NOT NULL DEFAULT '[]',
    staff_json       TEXT    NOT NULL DEFAULT '{}',
    links_json       TEXT    NOT NULL DEFAULT '{}',
    passwords_json   TEXT    NOT NULL DEFAULT '[]',
    extra_json       TEXT    NOT NULL DEFAULT '{}',
    parse_notes_json TEXT    NOT NULL DEFAULT '[]',

    has_photo        INTEGER NOT NULL DEFAULT 0,
    raw_text         TEXT    NOT NULL DEFAULT '',
    parse_status     TEXT    NOT NULL DEFAULT 'ok',
    ingested_at      TEXT    NOT NULL,

    PRIMARY KEY (channel_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts (parse_status);
CREATE INDEX IF NOT EXISTS idx_posts_posted ON posts (posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_score  ON posts (score DESC);
CREATE INDEX IF NOT EXISTS idx_posts_title  ON posts (title_cn);

CREATE TABLE IF NOT EXISTS post_tags (
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    kind       TEXT    NOT NULL,   -- 'tag'(#奇幻) | 'index'(#W 拼音首字母)
    tag        TEXT    NOT NULL,
    PRIMARY KEY (channel_id, message_id, kind, tag)
);

CREATE INDEX IF NOT EXISTS idx_tags_lookup ON post_tags (kind, tag);

-- 每次回填/同步的审计记录，用来发现解析成功率的回退
CREATE TABLE IF NOT EXISTS ingest_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    trace_id    TEXT    NOT NULL DEFAULT '',
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    total       INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 0,
    partial     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    upserted    INTEGER NOT NULL DEFAULT 0,
    note        TEXT    NOT NULL DEFAULT ''
);
