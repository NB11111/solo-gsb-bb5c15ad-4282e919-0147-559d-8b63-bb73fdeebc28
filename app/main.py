"""FastAPI 入口：REST API + 静态前端托管。

启动时自动建库（含演示数据），因此
`docker compose up --build` 一条命令即可完成依赖安装与数据库初始化。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .db import get_conn, get_setting, normalize_institution, normalize_topics, seed_demo_data, set_setting, transaction
from .schemas import (
    ConflictIn,
    LockIn,
    ManuscriptIn,
    ManuscriptPatch,
    ReviewerIn,
    ReviewerPatch,
    SettingsIn,
)
from .solver import institution_lock_exceeds, solve

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 依赖已在镜像构建/本地安装阶段就绪；这里完成数据库初始化
    db.init_db(seed_demo=os.environ.get("SEED_DEMO", "1") != "0")
    yield


app = FastAPI(title="匿名评审分配系统", lifespan=lifespan)


# ------------------------------------------------------------- 状态序列化
def build_state(conn: sqlite3.Connection) -> dict:
    def load_manuscripts():
        return [
            {"id": r["id"], "title": r["title"],
             "topics": json.loads(r["topics"]),
             "required_reviews": r["required_reviews"],
             "required_chairs": r["required_chairs"]}
            for r in conn.execute("SELECT * FROM manuscripts ORDER BY id").fetchall()
        ]

    def load_reviewers():
        return [
            {"id": r["id"], "name": r["name"],
             "topics": json.loads(r["topics"]), "capacity": r["capacity"],
             "institution": r["institution"] or "",
             "can_chair": bool(r["can_chair"])}
            for r in conn.execute("SELECT * FROM reviewers ORDER BY id").fetchall()
        ]

    assignments = [
        {"id": r["id"], "manuscript_id": r["m_id"],
         "reviewer_id": r["r_id"], "locked": bool(r["locked"])}
        for r in conn.execute(
            "SELECT * FROM assignments ORDER BY locked DESC, m_id, r_id")
    ]
    conflicts = [
        {"manuscript_id": r["m_id"], "reviewer_id": r["r_id"]}
        for r in conn.execute("SELECT m_id, r_id FROM conflicts ORDER BY m_id, r_id")
    ]

    load: dict[int, int] = {}
    locked_load: dict[int, int] = {}
    for a in assignments:
        load[a["reviewer_id"]] = load.get(a["reviewer_id"], 0) + 1
        if a["locked"]:
            locked_load[a["reviewer_id"]] = locked_load.get(a["reviewer_id"], 0) + 1

    ms_list = load_manuscripts()
    rv_list = load_reviewers()
    for rv in rv_list:
        rv["assigned"] = load.get(rv["id"], 0)
        rv["locked_assigned"] = locked_load.get(rv["id"], 0)

    per_m: dict[int, int] = {}
    for a in assignments:
        per_m[a["manuscript_id"]] = per_m.get(a["manuscript_id"], 0) + 1
    for m in ms_list:
        m["assigned"] = per_m.get(m["id"], 0)

    solution = get_setting(conn, "last_solution")

    return {
        "settings": {
            "default_required_reviews": get_setting(conn, "default_required_reviews"),
            "default_capacity": get_setting(conn, "default_capacity"),
            "mismatch_weight": get_setting(conn, "mismatch_weight"),
            "load_weight": get_setting(conn, "load_weight"),
            "institution_cap": get_setting(conn, "institution_cap"),
        },
        "manuscripts": ms_list,
        "reviewers": rv_list,
        "conflicts": conflicts,
        "assignments": assignments,
        "solution": solution,
    }


def _get_or_404(conn: sqlite3.Connection, table: str, obj_id: int, label: str) -> None:
    row = conn.execute(f"SELECT id FROM {table} WHERE id = ?", (obj_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{label} #{obj_id} 不存在")


# ----------------------------------------------------------------- 聚合接口
@app.get("/api/state")
def api_state(conn=Depends(get_conn)):
    return build_state(conn)


# ----------------------------------------------------------------- 稿件
@app.post("/api/manuscripts")
def create_manuscript(body: ManuscriptIn, conn=Depends(get_conn)):
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO manuscripts(title, topics, required_reviews, required_chairs) "
            "VALUES (?, ?, ?, ?)",
            (body.title.strip(),
             json.dumps(normalize_topics(body.topics), ensure_ascii=False),
             body.required_reviews,
             body.required_chairs),
        )
    return build_state(conn) | {"created_id": cur.lastrowid}


@app.patch("/api/manuscripts/{m_id}")
def update_manuscript(m_id: int, body: ManuscriptPatch, conn=Depends(get_conn)):
    _get_or_404(conn, "manuscripts", m_id, "稿件")
    fields, params = [], []
    if body.title is not None:
        fields.append("title = ?")
        params.append(body.title.strip())
    if body.topics is not None:
        fields.append("topics = ?")
        params.append(json.dumps(normalize_topics(body.topics), ensure_ascii=False))
    if body.required_reviews is not None:
        fields.append("required_reviews = ?")
        params.append(body.required_reviews)
    if body.required_chairs is not None:
        fields.append("required_chairs = ?")
        params.append(body.required_chairs)
    if not fields:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    # 合并更新后的主审人数不得超过所需评审总数
    row = conn.execute(
        "SELECT required_reviews, required_chairs FROM manuscripts WHERE id = ?",
        (m_id,),
    ).fetchone()
    req = body.required_reviews if body.required_reviews is not None else row["required_reviews"]
    chairs = body.required_chairs if body.required_chairs is not None else row["required_chairs"]
    if chairs > req:
        raise HTTPException(status_code=400, detail="所需主审人数不得超过所需评审总数")
    params.append(m_id)
    with transaction(conn):
        conn.execute(f"UPDATE manuscripts SET {', '.join(fields)} WHERE id = ?", params)
    return build_state(conn)


@app.delete("/api/manuscripts/{m_id}")
def delete_manuscript(m_id: int, conn=Depends(get_conn)):
    _get_or_404(conn, "manuscripts", m_id, "稿件")
    with transaction(conn):
        conn.execute("DELETE FROM manuscripts WHERE id = ?", (m_id,))
    return build_state(conn)


# ----------------------------------------------------------------- 评审人
@app.post("/api/reviewers")
def create_reviewer(body: ReviewerIn, conn=Depends(get_conn)):
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO reviewers(name, topics, capacity, institution, can_chair) "
            "VALUES (?, ?, ?, ?, ?)",
            (body.name.strip(),
             json.dumps(normalize_topics(body.topics), ensure_ascii=False),
             body.capacity,
             normalize_institution(body.institution),
             1 if body.can_chair else 0),
        )
    return build_state(conn) | {"created_id": cur.lastrowid}


@app.patch("/api/reviewers/{r_id}")
def update_reviewer(r_id: int, body: ReviewerPatch, conn=Depends(get_conn)):
    _get_or_404(conn, "reviewers", r_id, "评审人")
    fields, params = [], []
    if body.name is not None:
        fields.append("name = ?")
        params.append(body.name.strip())
    if body.topics is not None:
        fields.append("topics = ?")
        params.append(json.dumps(normalize_topics(body.topics), ensure_ascii=False))
    if body.capacity is not None:
        fields.append("capacity = ?")
        params.append(body.capacity)
    if body.institution is not None:
        fields.append("institution = ?")
        params.append(normalize_institution(body.institution))
    if body.can_chair is not None:
        fields.append("can_chair = ?")
        params.append(1 if body.can_chair else 0)
    if not fields:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    params.append(r_id)
    with transaction(conn):
        conn.execute(f"UPDATE reviewers SET {', '.join(fields)} WHERE id = ?", params)
    return build_state(conn)


@app.delete("/api/reviewers/{r_id}")
def delete_reviewer(r_id: int, conn=Depends(get_conn)):
    _get_or_404(conn, "reviewers", r_id, "评审人")
    with transaction(conn):
        conn.execute("DELETE FROM reviewers WHERE id = ?", (r_id,))
    return build_state(conn)


# ----------------------------------------------------------------- 冲突
@app.post("/api/conflicts")
def create_conflict(body: ConflictIn, conn=Depends(get_conn)):
    _get_or_404(conn, "manuscripts", body.manuscript_id, "稿件")
    _get_or_404(conn, "reviewers", body.reviewer_id, "评审人")
    locked = conn.execute(
        "SELECT 1 FROM assignments WHERE m_id = ? AND r_id = ? AND locked = 1",
        (body.manuscript_id, body.reviewer_id),
    ).fetchone()
    if locked:
        raise HTTPException(
            status_code=409,
            detail="该稿件-评审人存在锁定的既有分配，不能添加冲突；请先解锁。",
        )
    with transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO conflicts(m_id, r_id) VALUES (?, ?)",
            (body.manuscript_id, body.reviewer_id),
        )
    return build_state(conn)


@app.delete("/api/conflicts/{m_id}/{r_id}")
def delete_conflict(m_id: int, r_id: int, conn=Depends(get_conn)):
    with transaction(conn):
        conn.execute("DELETE FROM conflicts WHERE m_id = ? AND r_id = ?", (m_id, r_id))
    return build_state(conn)


# ----------------------------------------------------------------- 设置
@app.put("/api/settings")
def update_settings(body: SettingsIn, conn=Depends(get_conn)):
    data = body.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="没有需要更新的设置")
    with transaction(conn):
        for key, value in data.items():
            set_setting(conn, key, value)
    return build_state(conn)


# ----------------------------------------------------------------- 求解 / 锁定
@app.post("/api/solve")
def api_solve(conn=Depends(get_conn)):
    solution = solve(conn)
    return build_state(conn) | {"solution": solution}


@app.post("/api/assignments/lock")
def toggle_lock(body: LockIn, conn=Depends(get_conn)):
    _get_or_404(conn, "manuscripts", body.manuscript_id, "稿件")
    _get_or_404(conn, "reviewers", body.reviewer_id, "评审人")

    is_conflict = conn.execute(
        "SELECT 1 FROM conflicts WHERE m_id = ? AND r_id = ?",
        (body.manuscript_id, body.reviewer_id),
    ).fetchone()

    if body.locked:
        if is_conflict:
            raise HTTPException(status_code=409, detail="存在作者冲突的分配不能锁定。")
        req = conn.execute(
            "SELECT required_reviews FROM manuscripts WHERE id = ?",
            (body.manuscript_id,),
        ).fetchone()["required_reviews"]
        locked_n = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments WHERE m_id = ? AND locked = 1",
            (body.manuscript_id,),
        ).fetchone()["c"]
        existing = conn.execute(
            "SELECT locked FROM assignments WHERE m_id = ? AND r_id = ?",
            (body.manuscript_id, body.reviewer_id),
        ).fetchone()
        # 已存在的行只是被转为锁定（不增加人数）；否则新增一把锁会占用一个名额
        if existing is None and locked_n >= req:
            raise HTTPException(
                status_code=409,
                detail=f"该稿件最多需要 {req} 名评审，已锁定 {locked_n} 人，"
                       f"再锁定一名新评审人将超出需求；请先解锁一把锁。",
            )
        # 机构多样性硬约束：新增锁定会使该稿同机构人数超过上限时以 409 拒绝。
        institution = conn.execute(
            "SELECT COALESCE(institution, '') AS institution FROM reviewers WHERE id = ?",
            (body.reviewer_id,),
        ).fetchone()["institution"]
        org_cap = int(get_setting(conn, "institution_cap"))
        if institution and existing is None and org_cap > 0:
            same_inst_locked = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM assignments a JOIN reviewers rv ON a.r_id = rv.id
                WHERE a.m_id = ? AND a.locked = 1
                  AND COALESCE(rv.institution, '') = ?
                """,
                (body.manuscript_id, institution),
            ).fetchone()["c"]
            if institution_lock_exceeds(org_cap, same_inst_locked, True):
                raise HTTPException(
                    status_code=409,
                    detail=f"该稿件来自「{institution}」的锁定评审已有 {same_inst_locked} 人，"
                           f"再锁定将超过每篇同机构最多 {org_cap} 人的限制；"
                           f"请先解锁一名该机构的锁定，或在设置中放宽上限。",
                )
        with transaction(conn):
            conn.execute(
                "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 1) "
                "ON CONFLICT(m_id, r_id) DO UPDATE SET locked = 1",
                (body.manuscript_id, body.reviewer_id),
            )
    else:
        with transaction(conn):
            conn.execute(
                "DELETE FROM assignments WHERE m_id = ? AND r_id = ? AND locked = 1",
                (body.manuscript_id, body.reviewer_id),
            )

    # 锁定状态变化后立即重新求解：锁定边占用容量并参与负载计算
    solution = solve(conn)
    return build_state(conn) | {"solution": solution}


# ----------------------------------------------------------------- 演示/重置
@app.post("/api/demo")
def load_demo(conn=Depends(get_conn)):
    with transaction(conn):
        conn.execute("DELETE FROM assignments")
        conn.execute("DELETE FROM conflicts")
        conn.execute("DELETE FROM manuscripts")
        conn.execute("DELETE FROM reviewers")
        seed_demo_data(conn)
        set_setting(conn, "last_solution", None)
    solution = solve(conn)
    return build_state(conn) | {"solution": solution}


@app.post("/api/reset")
def reset_all(conn=Depends(get_conn)):
    with transaction(conn):
        conn.execute("DELETE FROM assignments")
        conn.execute("DELETE FROM conflicts")
        conn.execute("DELETE FROM manuscripts")
        conn.execute("DELETE FROM reviewers")
        set_setting(conn, "last_solution", None)
    return build_state(conn)


# ----------------------------------------------------------------- 静态前端
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
