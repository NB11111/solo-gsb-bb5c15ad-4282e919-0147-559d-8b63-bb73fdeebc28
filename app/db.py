"""SQLite 数据访问层。

单文件 SQLite，连接通过 FastAPI 依赖注入获取。
主题以 JSON 数组字符串存储（如 '["机器学习", "NLP"]'）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "review.db"))

DEFAULTS = {
    "default_required_reviews": 3,   # 新建稿件默认所需评审数
    "default_capacity": 3,           # 新建评审人默认个人容量
    "mismatch_weight": 10,           # 主题不匹配单位费用
    "load_weight": 2,                # 负载边际递增单位费用
    "institution_cap": 0,            # 每篇稿件同机构最多人数（0 关闭机构多样性约束）
    "last_solution": None,           # 最近一次求解结果（JSON）
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_conn():
    """FastAPI 依赖：每次请求一个连接。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS manuscripts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    topics           TEXT NOT NULL DEFAULT '[]',
    required_reviews INTEGER NOT NULL DEFAULT 3,
    required_chairs  INTEGER NOT NULL DEFAULT 0   -- 所需主审人数（0~2，且不得超过 required_reviews；0=不要求主审）
);

CREATE TABLE IF NOT EXISTS reviewers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    topics      TEXT NOT NULL DEFAULT '[]',
    capacity    INTEGER NOT NULL DEFAULT 3,
    institution TEXT NOT NULL DEFAULT '',  -- 可选所属机构；空串表示未填写（不计入机构上限）
    can_chair   INTEGER NOT NULL DEFAULT 0 -- 是否具备“可任主审”资格（0/1）
);

-- 作者冲突：稿件 m_id 与评审人 r_id 之间绝不允许出现分配边
CREATE TABLE IF NOT EXISTS conflicts (
    m_id INTEGER NOT NULL REFERENCES manuscripts(id) ON DELETE CASCADE,
    r_id INTEGER NOT NULL REFERENCES reviewers(id) ON DELETE CASCADE,
    PRIMARY KEY (m_id, r_id)
);

-- 既有分配（求解结果或锁定记录）
CREATE TABLE IF NOT EXISTS assignments (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    m_id   INTEGER NOT NULL REFERENCES manuscripts(id) ON DELETE CASCADE,
    r_id   INTEGER NOT NULL REFERENCES reviewers(id) ON DELETE CASCADE,
    locked INTEGER NOT NULL DEFAULT 0,
    UNIQUE (m_id, r_id)
);
"""


def init_db(seed_demo: bool = True) -> None:
    """建库建表、写入默认设置；空库时可选择载入演示数据。"""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, None if value is None else str(value)),
            )
        conn.commit()

        if seed_demo and conn.execute("SELECT COUNT(*) AS c FROM manuscripts").fetchone()["c"] == 0:
            seed_demo_data(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """对既有数据库做幂等的轻量迁移（保留现有数据）。

    - reviewers.institution：评审人可选机构（旧库补列，默认空串=未填写）。
    - reviewers.can_chair：评审人“可任主审”资格（旧库补列，默认 0=不具备）。
    - manuscripts.required_chairs：每稿所需主审人数（旧库补列，默认 0=不要求主审，
      即升级后主审硬约束默认关闭，行为与旧版本一致）。
    新版本引入的设置项由 init_db 中的 INSERT OR IGNORE 补默认值。
    """
    rv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(reviewers)")}
    if "institution" not in rv_cols:
        conn.execute("ALTER TABLE reviewers ADD COLUMN institution TEXT NOT NULL DEFAULT ''")
    if "can_chair" not in rv_cols:
        conn.execute("ALTER TABLE reviewers ADD COLUMN can_chair INTEGER NOT NULL DEFAULT 0")

    m_cols = {r["name"] for r in conn.execute("PRAGMA table_info(manuscripts)")}
    if "required_chairs" not in m_cols:
        conn.execute("ALTER TABLE manuscripts ADD COLUMN required_chairs INTEGER NOT NULL DEFAULT 0")


def get_setting(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return DEFAULTS.get(key)
    if key in ("default_required_reviews", "default_capacity",
               "mismatch_weight", "load_weight", "institution_cap"):
        return int(row["value"])
    if key == "last_solution":
        return json.loads(row["value"])
    return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, None if value is None else str(value)),
    )


def normalize_topics(raw: str | list[str]) -> list[str]:
    """将逗号/顿号分隔的字符串或列表规范化为去空白、去重、保序的小写主题列表。"""
    if isinstance(raw, list):
        items = raw
    else:
        items = raw.replace("、", ",").replace("，", ",").split(",")
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        t = item.strip().lower()
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def normalize_institution(raw: str | None) -> str:
    """机构为可选自由文本：仅去除首尾空白；空串表示未填写（不计入机构上限）。"""
    return (raw or "").strip()


def seed_demo_data(conn: sqlite3.Connection) -> None:
    """载入一组用于快速体验的演示数据。

    含两个刻意制造的未满足样例：
    - M4（体系结构/分布式系统）：相容评审仅钱教授 1 名 -> 总人数缺口；
    - M2（图神经网络推荐系统）：要求 2 名主审，全库该方向仅赵博士 1 名合资格 ->
      主审缺口（总人数仍可补齐），演示“主审缺口与总人数缺口分别给出”。
    """
    # (标题, 主题, 所需评审数, 所需主审人数)
    ms = [
        ("基于深度学习的机器翻译", ["机器学习", "nlp"], 3, 1),
        ("图神经网络推荐系统", ["机器学习", "推荐系统"], 3, 2),
        ("分布式数据库一致性协议", ["数据库", "分布式系统"], 3, 1),
        ("低功耗边缘计算架构", ["体系结构", "分布式系统"], 3, 1),
        ("大模型安全对齐", ["nlp", "安全"], 3, 1),
    ]
    # (姓名, 主题, 容量, 机构, 可任主审)
    rs = [
        # 机构仅为演示机构多样性约束：清华 3 人、北大 1 人、中科院 1 人；
        # 孙老师未填机构，不受“每篇同机构最多人数”限制（但其容量为 0）。
        # 张教授资历尚浅、不具主审资格：于是 M1 的主审由李研究员担任；
        # M2（机器学习/推荐系统）方向上具主审资格的只有赵博士 1 名，
        # 而 M2 要求 2 名主审 -> 主审缺口（总人数仍可补齐）。
        ("张教授", ["机器学习", "nlp"], 3, "清华大学", 0),
        ("李研究员", ["nlp", "安全"], 3, "清华大学", 1),
        # 王老师只擅长数据库（不含“分布式系统”），且与 M4 冲突；不具备主审资格。
        # 于是 M4（体系结构/分布式系统）只有钱教授 1 名相容评审，
        # 钱教授容量仅 3、需求 3，在机构上限下仍只够 1 名，演示“未满足稿件与原因”。
        ("王老师", ["数据库"], 3, "北京大学", 0),
        # 赵博士是唯一覆盖“推荐系统”且具主审资格的评审：M2 需要 2 名主审却只有他 1 名。
        ("赵博士", ["推荐系统", "机器学习"], 2, "清华大学", 1),
        ("钱教授", ["体系结构", "分布式系统"], 3, "中科院", 1),
        # 孙老师容量 0：全部容量之和 14 < 需求之和 15，形成全局性容量不足；
        # 无主审资格，但可作为普通评审（容量为 0 时不会被分稿）。
        ("孙老师", ["安全"], 0, "", 0),
    ]
    with transaction(conn):
        # 若在已有库上重新载入演示数据，重置自增序列，保证下面的 ID 引用确定。
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
        m_ids: list[int] = []
        for title, topics, req, chairs in ms:
            cur = conn.execute(
                "INSERT INTO manuscripts(title, topics, required_reviews, required_chairs) "
                "VALUES (?, ?, ?, ?)",
                (title, json.dumps(topics, ensure_ascii=False), req, chairs),
            )
            m_ids.append(cur.lastrowid)
        r_ids: list[int] = []
        for name, topics, cap, institution, can_chair in rs:
            cur = conn.execute(
                "INSERT INTO reviewers(name, topics, capacity, institution, can_chair) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, json.dumps(topics, ensure_ascii=False), cap, institution, can_chair),
            )
            r_ids.append(cur.lastrowid)
        # M4（体系结构/分布式）与除钱教授外的所有评审人冲突，
        # 形成“无冲突且相容的评审仅 1 名”的未满足样例。
        m4 = m_ids[3]
        conflicts = [(m4, rid) for rid in r_ids if rid != r_ids[4]]
        for m_id, r_id in conflicts:
            conn.execute("INSERT OR IGNORE INTO conflicts(m_id, r_id) VALUES (?, ?)", (m_id, r_id))
        # 演示机构多样性约束：每篇稿件同机构最多 2 人。
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('institution_cap', '2') "
            "ON CONFLICT(key) DO UPDATE SET value = '2'"
        )
