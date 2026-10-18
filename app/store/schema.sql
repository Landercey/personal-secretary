-- 私人秘书 · 数据库结构
--
-- 六张业务表 + 两本账（废件箱、事件日志）。
-- 时间是字符串 "YYYY-MM-DD HH:MM:SS"（和契约层保持一致，不做类型转换）。
--
-- 为什么不用 ORM：sqlite3 是标准库，SQL 一眼能看懂；
-- 换 ORM 多一层依赖，还得在 4 GB 机器上多跑一个包。

PRAGMA journal_mode = WAL;      -- 读写不互相阻塞
PRAGMA foreign_keys = ON;

-- 1. 来源（群 / 邮箱 / 手动）
CREATE TABLE IF NOT EXISTS sources (
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    first_seen  TEXT NOT NULL DEFAULT '',
    last_seen   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, name)
);

-- 2. 消息（原始进来的一条东西）
CREATE TABLE IF NOT EXISTS messages (
    msg_id       TEXT PRIMARY KEY,
    source_kind  TEXT NOT NULL,
    source_name  TEXT NOT NULL DEFAULT '',
    sender       TEXT NOT NULL DEFAULT '',
    is_self      INTEGER NOT NULL DEFAULT 0,
    sent_at      TEXT NOT NULL DEFAULT '',
    text         TEXT NOT NULL DEFAULT '',
    raw          TEXT NOT NULL DEFAULT '',      -- JSON，只用于排查
    created_at   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source_kind, source_name, sent_at);

-- 3. 待办
CREATE TABLE IF NOT EXISTS tasks (
    task_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    due_at        TEXT,
    location      TEXT,
    -- 提醒力度：截止前多少分钟单独提醒一次；0 = 只在每日汇总里出现
    -- （2026-10-13 之前这里是 1/2/3 三个档，open_database 会一次性把老值归零）
    remind_level  INTEGER NOT NULL DEFAULT 0,
    -- 一次提醒不够时用这个：**逗号分隔的分钟数**，比如 "60,10" = 提前一小时和
    -- 提前十分钟各提醒一次。没填就走上面那个单值（老数据全靠它）。
    -- 为什么另开一列而不是改上面那列：SQLite 改不了列类型，加列才是安全的迁移。
    remind_leads  TEXT NOT NULL DEFAULT '',
    priority      INTEGER NOT NULL DEFAULT 2,
    source_kind   TEXT NOT NULL DEFAULT 'manual',
    source_name   TEXT NOT NULL DEFAULT '',
    sender        TEXT NOT NULL DEFAULT '',
    origin_msg_id TEXT NOT NULL DEFAULT '',
    confidence    REAL NOT NULL DEFAULT 1.0,
    quoted        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open',      -- open / done / dropped
    is_archived   INTEGER NOT NULL DEFAULT 0,
    dedup_key     TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT '',
    completed_at  TEXT NOT NULL DEFAULT '',
    archive_reason TEXT NOT NULL DEFAULT '',
    drop_reason   TEXT NOT NULL DEFAULT '',
    -- 1 = 后台 AI 正在精修这条（界面显示「AI 整理中…」）。规则版先入库，
    -- AI 回来再改写标题；改完清零。
    ai_pending    INTEGER NOT NULL DEFAULT 0
    -- 子待办：指向父待办。只允许一层（父的 parent_id 必须是空）。
    ,parent_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, is_archived);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_dedup ON tasks(dedup_key) WHERE dedup_key <> '';

-- 4. 附件
CREATE TABLE IF NOT EXISTS attachments (
    attachment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER,
    msg_id        TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL DEFAULT '',
    mime          TEXT NOT NULL DEFAULT '',
    size          INTEGER NOT NULL DEFAULT 0,
    kind          TEXT NOT NULL DEFAULT 'other',
    sha256        TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_attachments_task ON attachments(task_id);

-- 5. 推送记录（去重：同一条待办不重复推）
CREATE TABLE IF NOT EXISTS push_records (
    push_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER,
    channel     TEXT NOT NULL DEFAULT '',
    batch       TEXT NOT NULL DEFAULT '',      -- 同一批汇总推送共用一个编号
    pushed_at   TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_push_task ON push_records(task_id, pushed_at);

-- 6. 设置快照（记住用户最后一次的选择，界面打开时不用重算）
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- 7. 废件箱：被判为碎片/噪声的东西。
--    不推送、不进主列表，但一定要留 —— 这些记录是日后调规则的真实案例，
--    也是"用户说这条不是碎片"时的翻案依据。
CREATE TABLE IF NOT EXISTS dropped_items (
    drop_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    msg_id     TEXT NOT NULL DEFAULT '',
    dropped_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dropped_time ON dropped_items(dropped_at);

-- 8. 事件日志：待办的每一次状态变化。出问题时能回答"它怎么变成这样的"。
CREATE TABLE IF NOT EXISTS task_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER,
    kind       TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, created_at);
