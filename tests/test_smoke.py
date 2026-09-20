"""最小冒烟测试：验证求解器核心约束与前端服务可用性。

运行：python -m tests.test_smoke
（仅使用标准库，不依赖 pytest）
"""
from __future__ import annotations

import json
import os
import tempfile

# 测试前指定独立数据库
_tmp = tempfile.mkdtemp(prefix="review-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app.db import _connect, init_db, set_setting  # noqa: E402
from app.mcmf import MinCostMaxFlow  # noqa: E402
from app.solver import institution_lock_exceeds, solve  # noqa: E402


def test_mcmf_known_case() -> None:
    net = MinCostMaxFlow(6)
    net.add_edge(0, 1, 2, 0)
    net.add_edge(0, 2, 2, 0)
    net.add_edge(1, 3, 1, 1)
    net.add_edge(1, 4, 2, 3)
    net.add_edge(2, 3, 2, 1)
    net.add_edge(3, 5, 2, 2)
    net.add_edge(4, 5, 2, 1)
    flow, cost = net.solve(0, 5)
    assert (flow, cost) == (4, 14)


def test_demo_solve_constraints() -> None:
    init_db(seed_demo=True)
    conn = _connect()
    try:
        sol = solve(conn)
        # 演示数据刻意制造 2 篇未满足：
        # M2 主审缺口（总人数够）、M4 总人数缺口（相容评审仅 1 名）
        assert not sol["fully_satisfied"]
        assert len(sol["unmet"]) == 2
        unmet_by_id = {u["manuscript_id"]: u for u in sol["unmet"]}
        assert unmet_by_id[2]["deficit"] == 0 and unmet_by_id[2]["chair_deficit"] == 1
        assert unmet_by_id[4]["deficit"] == 2

        # 冲突边绝不出现在分配中
        conflicts = {
            (r["m_id"], r["r_id"])
            for r in conn.execute("SELECT m_id, r_id FROM conflicts")
        }
        used = {
            (r["m_id"], r["r_id"])
            for r in conn.execute("SELECT m_id, r_id FROM assignments")
        }
        assert not (conflicts & used)

        # 每稿不超需求、每人不超容量（无锁定时）
        req = {r["id"]: r["required_reviews"]
               for r in conn.execute("SELECT id, required_reviews FROM manuscripts")}
        cap = {r["id"]: r["capacity"]
               for r in conn.execute("SELECT id, capacity FROM reviewers")}
        per_m: dict[int, int] = {}
        per_r: dict[int, int] = {}
        for m_id, r_id in used:
            per_m[m_id] = per_m.get(m_id, 0) + 1
            per_r[r_id] = per_r.get(r_id, 0) + 1
        assert all(per_m.get(m, 0) <= n for m, n in req.items())
        assert all(per_r.get(r, 0) <= n for r, n in cap.items())

        # 演示数据开启机构上限（每稿同机构最多 2 人）：所有稿件均不得超限
        assert sol["institution_cap"] == 2
        inst = {r["id"]: r["institution"]
                for r in conn.execute("SELECT id, institution FROM reviewers")}
        per_m_inst: dict[tuple[int, str], int] = {}
        for m_id, r_id in used:
            if inst[r_id]:
                key = (m_id, inst[r_id])
                per_m_inst[key] = per_m_inst.get(key, 0) + 1
        assert all(n <= 2 for n in per_m_inst.values())

        # 每篇稿件都返回机构构成（内存解以 int 为键，JSON 持久化后为字符串键）
        for m_id in req:
            groups = sol["institutions"].get(m_id) or sol["institutions"].get(str(m_id))
            assert groups is not None
            assert sum(g["count"] for g in groups) == per_m.get(m_id, 0)
    finally:
        conn.close()


def test_locks_occupy_capacity() -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE assignments SET locked=1 WHERE m_id=1 AND r_id=2")
        conn.commit()
        sol = solve(conn)
        assert sol["locked_count"] == 1
        # 锁定预占被锁评审的一个容量槽位：其（锁定+自由）总数不超过容量，
        # 且锁定行确实保留
        cap_r2 = conn.execute(
            "SELECT capacity FROM reviewers WHERE id=2").fetchone()["capacity"]
        total_r2 = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE r_id=2").fetchone()["c"]
        locked_r2 = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE r_id=2 AND locked=1"
        ).fetchone()["c"]
        assert locked_r2 == 1
        assert total_r2 <= cap_r2
        # 锁定先占机构名额：该稿同机构（清华大学）最多还能再分配 1 人
        same_inst_free = conn.execute(
            """
            SELECT COUNT(*) AS c FROM assignments a
            JOIN reviewers rv ON a.r_id = rv.id
            JOIN manuscripts mv ON a.m_id = mv.id
            WHERE a.m_id = 1 AND rv.institution =
                  (SELECT institution FROM reviewers WHERE id = 2)
            """).fetchone()["c"]
        assert same_inst_free == 2  # 1 锁定 + 1 自由，恰好顶到机构上限
        conn.execute("DELETE FROM assignments WHERE locked=1")
        conn.commit()
    finally:
        conn.close()


def _make_scenario(conn, cap: int) -> None:
    """清空并构造：1 篇需求 3 的稿件；A 机构 3 人、B 机构 1 人、无机构 1 人。"""
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM manuscripts")
    conn.execute("DELETE FROM reviewers")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
    conn.execute(
        "INSERT INTO manuscripts(title, topics, required_reviews, required_chairs) "
        "VALUES (?, ?, ?, ?)",
        ("测试稿", json.dumps(["t"], ensure_ascii=False), 3, 0))
    for name, inst in [("a1", "A"), ("a2", "A"), ("a3", "A"), ("b1", "B"), ("x1", "")]:
        conn.execute(
            "INSERT INTO reviewers(name, topics, capacity, institution, can_chair) "
            "VALUES (?, ?, ?, ?, 0)",
            (name, json.dumps(["t"], ensure_ascii=False), 3, inst))
    set_setting(conn, "institution_cap", cap)
    conn.commit()


def test_institution_cap_hard_limit() -> None:
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        sol = solve(conn)
        # cap=1：A 机构至多 1 人；需求 3 由 A、B、无机构各 1 人满足
        per_inst: dict[str, int] = {}
        for r in conn.execute(
                "SELECT r_id FROM assignments WHERE m_id=1"):
            row = conn.execute(
                "SELECT institution FROM reviewers WHERE id=?", (r["r_id"],)).fetchone()
            per_inst[row["institution"]] = per_inst.get(row["institution"], 0) + 1
        assert per_inst.get("A", 0) == 1
        assert per_inst.get("B", 0) == 1
        assert per_inst.get("", 0) == 1
        assert sol["fully_satisfied"]

        # 关闭机构上限（0）：同机构不再受限
        _make_scenario(conn, 0)
        sol = solve(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM assignments WHERE m_id=1").fetchone()["c"]
        assert n == 3
        assert sol["institution_cap"] == 0
        assert all(not g.get("over_cap") for groups in sol["institutions"].values()
                   for g in groups)
    finally:
        conn.close()


def test_institution_lock_over_cap_warns_and_blocks() -> None:
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        # 历史锁定同一机构 2 人（绕过 API 直接构造“存量数据”）
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) "
            "SELECT 1, id, 1 FROM reviewers WHERE name IN ('a1','a2')")
        conn.commit()
        sol = solve(conn)
        # 锁定强制保留（2 名 A 仍在），机构超限告警，且不再向 A 机构分配新人
        locked_a = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments a JOIN reviewers rv ON a.r_id=rv.id "
            "WHERE a.m_id=1 AND rv.institution='A'").fetchone()["c"]
        assert locked_a == 2
        w = [x for x in sol["warnings"] if x["code"] == "institution_lock_over_cap"]
        assert len(w) == 1 and w[0]["institution"] == "A"
        groups_map = sol["institutions"].get(1) or sol["institutions"]["1"]
        groups = {g["institution"]: g for g in groups_map}
        assert groups["A"]["over_cap"] is True
        # 第 3 个名额只能给 B 或无机构，绝不会再出现第 3 个 A
        per_inst: dict[str, int] = {}
        for r in conn.execute("SELECT r_id FROM assignments WHERE m_id=1"):
            inst = conn.execute(
                "SELECT institution FROM reviewers WHERE id=?", (r["r_id"],)).fetchone()["institution"]
            per_inst[inst] = per_inst.get(inst, 0) + 1
        assert per_inst["A"] == 2

        # 新增一把同机构的锁：判定函数为真（API 层据此返回 409）
        assert institution_lock_exceeds(1, same_inst_locked=1, is_new_lock=True) is True
        # 仅转换已有行为锁、上限关闭或已满额不增员时均不拒绝
        assert institution_lock_exceeds(1, same_inst_locked=1, is_new_lock=False) is False
        assert institution_lock_exceeds(0, same_inst_locked=5, is_new_lock=True) is False
        assert institution_lock_exceeds(2, same_inst_locked=1, is_new_lock=True) is False
    finally:
        conn.close()


def test_no_institution_reviewers_uncounted() -> None:
    conn = _connect()
    try:
        _make_scenario(conn, 1)
        # B 机构锁 1 人已满；无机构评审不占名额，A 仍可再进 1 人
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) "
            "SELECT 1, id, 1 FROM reviewers WHERE name='b1'")
        conn.commit()
        sol = solve(conn)
        assert not any(w["code"] == "institution_lock_over_cap" for w in sol["warnings"])
        names = {
            r["name"] for r in conn.execute("""
                SELECT rv.name AS name FROM assignments a
                JOIN reviewers rv ON a.r_id = rv.id WHERE a.m_id=1""")
        }
        assert "b1" in names and "x1" in names
        assert sum(n.startswith("a") for n in names) == 1
    finally:
        conn.close()


def test_schema_migration_adds_institution() -> None:
    """旧库（reviewers 无 institution 列）启动迁移后自动补列且数据保留。"""
    path = os.path.join(tempfile.mkdtemp(prefix="review-mig-"), "old.db")
    from app import db
    old_path = db.DB_PATH
    db.DB_PATH = path
    try:
        import sqlite3
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE reviewers (id INTEGER PRIMARY KEY, name TEXT, topics TEXT, capacity INTEGER)")
        c.execute("INSERT INTO reviewers(name, topics, capacity) VALUES ('老评审','[]',3)")
        c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE manuscripts (id INTEGER PRIMARY KEY, title TEXT, topics TEXT, required_reviews INTEGER)")
        c.execute("CREATE TABLE conflicts (m_id INTEGER, r_id INTEGER)")
        c.execute("CREATE TABLE assignments (id INTEGER PRIMARY KEY, m_id INTEGER, r_id INTEGER, locked INTEGER)")
        c.commit()
        c.close()

        init_db(seed_demo=False)
        c2 = sqlite3.connect(path)
        c2.row_factory = sqlite3.Row
        row = c2.execute("SELECT name, institution, can_chair FROM reviewers WHERE name='老评审'").fetchone()
        assert row["institution"] == ""  # 既有数据保留，机构默认未填写
        assert row["can_chair"] == 0    # 旧评审默认不具主审资格
        assert c2.execute("SELECT value FROM settings WHERE key='institution_cap'").fetchone()["value"] == "0"
        c2.close()
    finally:
        db.DB_PATH = old_path


def _make_chair_scenario(conn, required: int, chairs: int,
                         qualified: set[str], capacities: dict[str, int] | None = None,
                         institution_cap: int = 0) -> None:
    """清空并构造 1 篇稿件（需求 required、主审 chairs）与 a1..a5 五名同主题评审；
    qualified 中的评审人具备主审资格。"""
    conn.execute("DELETE FROM assignments")
    conn.execute("DELETE FROM conflicts")
    conn.execute("DELETE FROM manuscripts")
    conn.execute("DELETE FROM reviewers")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
    conn.execute(
        "INSERT INTO manuscripts(title, topics, required_reviews, required_chairs) "
        "VALUES (?, ?, ?, ?)",
        ("主审稿", json.dumps(["t"], ensure_ascii=False), required, chairs))
    for name in ["a1", "a2", "a3", "a4", "a5"]:
        cap = (capacities or {}).get(name, 5)
        conn.execute(
            "INSERT INTO reviewers(name, topics, capacity, institution, can_chair) "
            "VALUES (?, ?, ?, '', ?)",
            (name, json.dumps(["t"], ensure_ascii=False), cap, 1 if name in qualified else 0))
    set_setting(conn, "institution_cap", institution_cap)
    conn.commit()


def test_chairs_filled_by_qualified() -> None:
    """主审名额由合资格评审人补齐；不合资格者不会被标记为主审。"""
    conn = _connect()
    try:
        _make_chair_scenario(conn, required=3, chairs=2, qualified={"a3", "a5"})
        sol = solve(conn)
        assert sol["fully_satisfied"]
        chairs = sol["chairs"].get(1) or sol["chairs"].get("1")
        assert chairs == [3, 5]  # 合资格者按 ID 确定性出任主审
        ids = {r["r_id"] for r in conn.execute("SELECT r_id FROM assignments WHERE m_id=1")}
        assert ids == {3, 5} | (ids - {3, 5})  # 共 3 人，主审是其中 2 名
        assert len(ids) == 3
        for key, ec in sol["edge_costs"].items():
            m_id = int(key.split(":")[0])
            if m_id == 1:
                assert ec["is_chair"] == (int(key.split(":")[1]) in {3, 5})
    finally:
        conn.close()


def test_chair_deficit_reported_separately() -> None:
    """主审不足但总人数够：仍给出最大可行分配，列入未满足，仅报主审缺口。"""
    conn = _connect()
    try:
        # 5 名候选中只有 a2、a3 合资格；a2 出任本稿主审，a3 容量为 0（被其他稿占满）。
        _make_chair_scenario(
            conn, required=3, chairs=2, qualified={"a2", "a3"},
            capacities={"a3": 0})
        sol = solve(conn)
        assert not sol["fully_satisfied"]
        assert len(sol["unmet"]) == 1
        u = sol["unmet"][0]
        assert u["deficit"] == 0
        assert u["chair_deficit"] == 1
        assert u["required_chairs"] == 2 and u["assigned_chairs"] == 1
        assert any("主审缺口 1 名" in r for r in u["reasons"])
        # 总人数仍是最大可行分配（3 名）
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE m_id=1").fetchone()["c"] == 3
        # 候选明细带资格与主审阻断原因（a2 已在本稿，不进入候选）
        cands = {c["name"]: c for c in u["details"]["candidates"]}
        assert "a2" not in cands
        # a3 合资格但容量 0 -> 主审阻断原因为 no_capacity
        assert cands["a3"]["can_chair"] is True
        assert cands["a3"]["chair_available"] is False
        assert cands["a3"]["chair_block_reason"] == "no_capacity"
        assert any(not c["can_chair"] and c["chair_block_reason"] == "not_qualified"
                   for c in cands.values())
    finally:
        conn.close()


def test_unqualified_cannot_be_chair_even_if_scarce() -> None:
    """无人合资格时：普通评审照常补齐总名额，主审缺口=所需主审数。"""
    conn = _connect()
    try:
        _make_chair_scenario(conn, required=2, chairs=1, qualified=set())
        sol = solve(conn)
        u = sol["unmet"][0]
        assert u["deficit"] == 0 and u["chair_deficit"] == 1
        assert sol["total_chairs_assigned"] == 0
        assert not any(ec["is_chair"] for ec in sol["edge_costs"].values())
    finally:
        conn.close()


def test_chair_fallback_keeps_max_assignment() -> None:
    """2 个主审名额但自由合资格者仅 1 人：主审缺 1、总人数仍最大，
    且主审名额回流为普通名额时不会被误计为主审。"""
    conn = _connect()
    try:
        _make_chair_scenario(conn, required=3, chairs=2, qualified={"a1"})
        sol = solve(conn)
        assert sol["total_assigned"] == 3
        assert sol["total_chairs_assigned"] == 1
        chairs = sol["chairs"].get(1) or sol["chairs"]["1"]
        assert chairs == [1]
        u = sol["unmet"][0]
        assert u["deficit"] == 0 and u["chair_deficit"] == 1
        # 费用还原（回流惩罚不出现在输出费用中）
        recomputed = sum(
            v["mismatch_cost"] + v["load_cost"]
            for k, v in sol["edge_costs"].items() if k.startswith("1:"))
        assert recomputed == sum(
            v["mismatch_cost"] + v["load_cost"] for v in sol["edge_costs"].values())
    finally:
        conn.close()


def test_locked_chairs_deducted_by_reviewer_id() -> None:
    """有效锁定按评审人 ID 升序优先抵扣主审名额；主审同样占用容量与机构名额。"""
    conn = _connect()
    try:
        # 机构上限 2：a1、a2 同属 A 机构且被锁定为主审，第三人不能再来自 A。
        conn.execute("DELETE FROM assignments")
        conn.execute("DELETE FROM conflicts")
        conn.execute("DELETE FROM manuscripts")
        conn.execute("DELETE FROM reviewers")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
        conn.execute(
            "INSERT INTO manuscripts(title, topics, required_reviews, required_chairs) "
            "VALUES (?, ?, ?, ?)",
            ("锁主审稿", json.dumps(["t"], ensure_ascii=False), 3, 2))
        for name, inst, q in [("a1", "A", 1), ("a2", "A", 1), ("a3", "A", 0),
                              ("b1", "B", 0)]:
            conn.execute(
                "INSERT INTO reviewers(name, topics, capacity, institution, can_chair) "
                "VALUES (?, ?, 5, ?, ?)",
                (name, json.dumps(["t"], ensure_ascii=False), inst, q))
        # 锁定 a1、a2（均合资格）-> 按 ID 升序抵扣 2 个主审名额
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) "
            "SELECT 1, id, 1 FROM reviewers WHERE name IN ('a1','a2')")
        set_setting(conn, "institution_cap", 2)
        conn.commit()
        sol = solve(conn)
        chairs = sol["chairs"].get(1) or sol["chairs"]["1"]
        assert chairs == [1, 2]
        assert sol["fully_satisfied"]
        # 第三人不得来自 A 机构（机构名额已被 2 个主审锁占满）
        third = conn.execute(
            """SELECT rv.institution AS inst FROM assignments a
               JOIN reviewers rv ON a.r_id=rv.id
               WHERE a.m_id=1 AND a.locked=0""").fetchone()
        assert third["inst"] == "B"
        # 主审锁定行被标识 is_chair
        assert sol["edge_costs"]["1:1"]["is_chair"] is True
        assert sol["edge_costs"]["1:2"]["is_chair"] is True
    finally:
        conn.close()


def test_locked_non_qualified_not_chair() -> None:
    """锁定了不具资格的人：不抵扣主审名额，主审由自由分配补齐，同一人不重复。"""
    conn = _connect()
    try:
        _make_chair_scenario(conn, required=3, chairs=1, qualified={"a3", "a4"})
        # 锁定 a1（不具资格）
        conn.execute(
            "INSERT INTO assignments(m_id, r_id, locked) "
            "SELECT 1, id, 1 FROM reviewers WHERE name='a1'")
        conn.commit()
        sol = solve(conn)
        assert sol["fully_satisfied"]
        chairs = sol["chairs"].get(1) or sol["chairs"]["1"]
        assert chairs == [3]  # 自由合资格者中 ID 最小者出任主审
        assert sol["edge_costs"]["1:1"]["is_chair"] is False
        ids = {r["r_id"] for r in conn.execute("SELECT r_id FROM assignments WHERE m_id=1")}
        assert len(ids) == 3 and 1 in ids and 3 in ids
    finally:
        conn.close()


def test_chairs_share_capacity_and_inst_quota() -> None:
    """主审同时占用个人容量（跨稿）与每稿机构名额。"""
    conn = _connect()
    try:
        conn.execute("DELETE FROM assignments")
        conn.execute("DELETE FROM conflicts")
        conn.execute("DELETE FROM manuscripts")
        conn.execute("DELETE FROM reviewers")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('manuscripts','reviewers')")
        for i, title in enumerate(["稿一", "稿二"], start=1):
            conn.execute(
                "INSERT INTO manuscripts(title, topics, required_reviews, required_chairs) "
                "VALUES (?, ?, ?, ?)",
                (title, json.dumps(["t"], ensure_ascii=False), 2, 1))
        # 唯一合资格主审 r1 容量 1：两稿主审竞争，仅一稿能拿到主审
        for name, cap, q in [("r1", 1, 1), ("r2", 5, 0), ("r3", 5, 0)]:
            conn.execute(
                "INSERT INTO reviewers(name, topics, capacity, institution, can_chair) "
                "VALUES (?, ?, ?, '', ?)",
                (name, json.dumps(["t"], ensure_ascii=False), cap, q))
        conn.commit()
        sol = solve(conn)
        assert sol["total_chairs_required"] == 2
        assert sol["total_chairs_assigned"] == 1
        chair_ms = [int(k) for k, v in sol["chairs"].items() if v]
        assert len(chair_ms) == 1
        # 拿不到主审的稿子列入未满足（总人数缺口为 0）
        unmet_ids = {u["manuscript_id"] for u in sol["unmet"]}
        assert unmet_ids <= {1, 2} and len(unmet_ids) == 1
        assert next(iter(unmet_ids)) not in chair_ms
    finally:
        conn.close()


def test_schema_migration_adds_chair_columns() -> None:
    """旧库（无 can_chair / required_chairs 列）启动迁移后自动补列且默认关闭主审约束。"""
    path = os.path.join(tempfile.mkdtemp(prefix="review-chair-"), "old.db")
    from app import db
    old_path = db.DB_PATH
    db.DB_PATH = path
    try:
        import sqlite3
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE reviewers (id INTEGER PRIMARY KEY, name TEXT, topics TEXT, "
                  "capacity INTEGER, institution TEXT)")
        c.execute("INSERT INTO reviewers(name, topics, capacity, institution) "
                  "VALUES ('老评审','[]',3,'')")
        c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE manuscripts (id INTEGER PRIMARY KEY, title TEXT, topics TEXT, "
                  "required_reviews INTEGER)")
        c.execute("INSERT INTO manuscripts(title, topics, required_reviews) "
                  "VALUES ('老稿件','[]',3)")
        c.execute("CREATE TABLE conflicts (m_id INTEGER, r_id INTEGER)")
        c.execute("CREATE TABLE assignments (id INTEGER PRIMARY KEY, m_id INTEGER, "
                  "r_id INTEGER, locked INTEGER)")
        c.commit()
        c.close()

        init_db(seed_demo=False)
        c2 = sqlite3.connect(path)
        c2.row_factory = sqlite3.Row
        rv = c2.execute("SELECT can_chair FROM reviewers WHERE name='老评审'").fetchone()
        mm = c2.execute("SELECT required_chairs FROM manuscripts WHERE title='老稿件'").fetchone()
        assert rv["can_chair"] == 0       # 旧评审默认不具主审资格
        assert mm["required_chairs"] == 0  # 旧稿件默认不需要主审（约束关闭）
        c2.close()
    finally:
        db.DB_PATH = old_path


if __name__ == "__main__":
    test_mcmf_known_case()
    test_demo_solve_constraints()
    test_locks_occupy_capacity()
    test_institution_cap_hard_limit()
    test_institution_lock_over_cap_warns_and_blocks()
    test_no_institution_reviewers_uncounted()
    test_schema_migration_adds_institution()
    test_chairs_filled_by_qualified()
    test_chair_deficit_reported_separately()
    test_unqualified_cannot_be_chair_even_if_scarce()
    test_chair_fallback_keeps_max_assignment()
    test_locked_chairs_deducted_by_reviewer_id()
    test_locked_non_qualified_not_chair()
    test_chairs_share_capacity_and_inst_quota()
    test_schema_migration_adds_chair_columns()
    print("所有冒烟测试通过 ✅")
