"""评审分配求解器：最小费用最大流。

网络模型
========
节点：源点 S -> {主审通道 C_i / 普通通道 N_i} -> [通道匹配 P^lane_{i,a}
-> 共享配额 Q_{i,a} -> U_{i,a} ->] 评审人 R_j -> 汇点 T

* S -> C_i：容量 = 该稿尚缺的主审数（主审需求扣除已锁定主审），只连接
  具备“可任主审”资格的评审人；
  S -> N_i：容量 = 尚缺总名额 - 尚缺主审数。两通道源点容量之和恰为尚缺
  总名额，故总分配数绝不超过需求；另有高费（2δ）回流边 C_i -> N_i，
  当合资格主审实在不足时把主审通道余量转为普通名额，从而仍返回最大可行
  分配，该稿则带着主审缺口进入未满足清单。
* 两条通道合流到同一组“每对容量 1”的分配边与同一组评审负载槽位，
  故同一评审对同一稿件至多分配一次，且主审与普通评审共享个人容量。
* 分配边：冲突边、与锁定分配重复的边一律不建。普通通道分配边在主题费
  之外加收主审优先惩罚 δ（大于任何单位流原始费用上界），主审通道不收，
  于是在“最大可行分配”优先前提下总是优先用主审通道补齐主审名额。
* 机构硬约束：开启 gamma（>0）且评审人填有机构 a 时，两通道分别经
  P^c/P^n 汇入唯一配额边 Q_{i,a} -> U_{i,a}（容量 =
  gamma - 该稿该机构锁定数）再接评审人；主审/普通共享同一机构名额。
  名额被锁定占满（或历史锁定已超限）时配额边不建。
  未填写机构的评审人走 C_i/N_i -> R_j 直接边，不受机构名额限制。
* R_j -> T：把容量拆成若干条单位“负载槽位”并行边，
  第 k 个槽位费用 = alpha * k（已占用 k = 锁定数 + 1 起算），
  从而把“评审负载偏差”做成凸的边际成本，流会天然趋向均衡。
* 分配边费用 = beta * 该评审未覆盖的稿件主题数（普通通道另加 delta；
  δ 与回流边 2δ 仅为算法优先级，输出费用全额扣除，见 solve 内还原逻辑）。

主审与普通评审一样同时占用稿件需求、评审个人容量与机构名额。
有效锁定中，合资格（对象存在、无冲突）的锁定按评审人 ID 升序优先
抵扣该稿主审名额；被指定为主审的锁定额外计入该稿已满足主审数。
冲突边根本不会被加入图中，因此绝不可能被使用。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .db import get_setting, set_setting, transaction
from .mcmf import MinCostMaxFlow


def _rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    return conn.execute(sql).fetchall()


def institution_lock_exceeds(cap: int, same_inst_locked: int, is_new_lock: bool) -> bool:
    """纯函数：新增锁定是否会使同机构人数超过每稿上限。

    cap=0 表示关闭机构多样性约束；仅“新增一行锁定”（此前不存在该分配）才占新名额，
    把已有自由分配转为锁定不增加人数。
    """
    if cap <= 0 or not is_new_lock:
        return False
    return same_inst_locked + 1 > cap


def solve(conn: sqlite3.Connection) -> dict:
    beta = int(get_setting(conn, "mismatch_weight"))   # 主题不匹配权重
    alpha = int(get_setting(conn, "load_weight"))      # 负载偏差权重
    org_cap = int(get_setting(conn, "institution_cap"))  # 每稿同机构最多人数（0=关闭）

    manuscripts = [dict(r) for r in _rows(
        conn, "SELECT * FROM manuscripts ORDER BY id")]
    reviewers = [dict(r) for r in _rows(
        conn, "SELECT * FROM reviewers ORDER BY id")]

    # 主审资格与每稿主审需求（required_chairs 恒 <= required_reviews，见表单/API 校验）
    can_chair_by_r: dict[int, bool] = {r["id"]: bool(r["can_chair"]) for r in reviewers}
    chairs_req: dict[int, int] = {m["id"]: int(m["required_chairs"] or 0) for m in manuscripts}

    conflict_pairs: set[tuple[int, int]] = {
        (r["m_id"], r["r_id"])
        for r in _rows(conn, "SELECT m_id, r_id FROM conflicts")
    }
    lock_rows = _rows(conn, "SELECT m_id, r_id FROM assignments WHERE locked = 1 ORDER BY m_id, r_id")

    m_map = {m["id"]: m for m in manuscripts}
    r_map = {r["id"]: r for r in reviewers}
    m_by_id = {m["id"]: {**m, "topics": json.loads(m["topics"])} for m in manuscripts}
    r_by_id = {r["id"]: {**r, "topics": json.loads(r["topics"])} for r in reviewers}

    warnings: list[dict] = []

    # ---------------------------------------------------------------- 锁定校验
    # 有效锁：稿件/评审人均存在、无冲突、且未超过该稿所需评审数。
    # 无效锁保留在表中（由用户解锁），但不参与本次求解。
    locks_by_m: dict[int, list[int]] = {}
    raw_pairs: list[tuple[int, int]] = []
    for row in lock_rows:
        m_id, r_id = row["m_id"], row["r_id"]
        raw_pairs.append((m_id, r_id))
        if m_id not in m_map or r_id not in r_map:
            warnings.append({
                "code": "lock_missing_entity",
                "message": f"锁定分配（稿件 #{m_id} / 评审人 #{r_id}）指向已删除对象，已忽略。",
                "manuscript_id": m_id, "reviewer_id": r_id,
            })
            continue
        if (m_id, r_id) in conflict_pairs:
            warnings.append({
                "code": "lock_conflict",
                "message": f"《{m_map[m_id]['title']}》与 {r_map[r_id]['name']} 存在作者冲突，该锁定被忽略，请先解除冲突或解锁。",
                "manuscript_id": m_id, "reviewer_id": r_id,
            })
            continue
        locks_by_m.setdefault(m_id, []).append(r_id)

    valid_locks: list[tuple[int, int]] = []
    for m_id, r_ids in locks_by_m.items():
        req = m_map[m_id]["required_reviews"]
        if len(r_ids) > req:
            for extra in r_ids[req:]:
                warnings.append({
                    "code": "lock_exceeds_required",
                    "message": f"《{m_map[m_id]['title']}》锁定了 {len(r_ids)} 人，超过每稿 {req} 人的需求，"
                               f"超出的锁定（{r_map[extra]['name']}）本次不计入。",
                    "manuscript_id": m_id, "reviewer_id": extra,
                })
        valid_locks.extend((m_id, r_id) for r_id in r_ids[:req])

    locked_pairs: set[tuple[int, int]] = set(valid_locks)
    locked_count: dict[int, int] = {}
    for _, r_id in valid_locks:
        locked_count[r_id] = locked_count.get(r_id, 0) + 1

    # 主审名额：每篇稿件的有效锁定按评审人 ID 升序排列，合资格（可任主审）的
    # 锁定优先抵扣主审名额；不足部分留给自由分配补齐。
    locked_chairs: set[tuple[int, int]] = set()
    locked_chair_count_by_m: dict[int, int] = {}
    locked_by_m_list: dict[int, list[int]] = {}
    for m_id, r_id in valid_locks:
        locked_by_m_list.setdefault(m_id, []).append(r_id)
    for m_id, r_ids in locked_by_m_list.items():
        for r_id in sorted(r_ids):
            if (locked_chair_count_by_m.get(m_id, 0) < chairs_req.get(m_id, 0)
                    and can_chair_by_r.get(r_id, False)):
                locked_chairs.add((m_id, r_id))
                locked_chair_count_by_m[m_id] = locked_chair_count_by_m.get(m_id, 0) + 1

    # 锁定数超过个人容量：强制保留（既有分配），剩余容量为 0，并给出警告。
    free_capacity: dict[int, int] = {}
    for r in reviewers:
        cap, lc = r["capacity"], locked_count.get(r["id"], 0)
        if lc > cap:
            warnings.append({
                "code": "reviewer_over_capacity",
                "message": f"{r['name']} 已有 {lc} 个锁定分配，超过个人容量 {cap}；"
                           f"锁定分配强制保留，本次不再向其安排新稿。",
                "reviewer_id": r["id"],
            })
            free_capacity[r["id"]] = 0
        else:
            free_capacity[r["id"]] = cap - lc

    # 机构名额：锁定分配先占用每篇稿件的同机构名额。
    # 历史锁定已超过机构上限时：保留锁定、发出警告、本次不再向该稿分配该机构的人。
    # 未填写机构（空串）的评审人不参与机构统计。
    inst_by_r: dict[int, str] = {r["id"]: (r["institution"] or "").strip() for r in reviewers}
    locked_inst_count: dict[tuple[int, str], int] = {}
    for m_id, r_id in valid_locks:
        inst = inst_by_r.get(r_id, "")
        if inst:
            locked_inst_count[(m_id, inst)] = locked_inst_count.get((m_id, inst), 0) + 1

    inst_over: set[tuple[int, str]] = set()  # 历史锁定已超限/占满机构名额的（稿件, 机构）
    if org_cap > 0:
        for (m_id, inst), cnt in sorted(locked_inst_count.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            if cnt > org_cap:
                inst_over.add((m_id, inst))
                over_names = [
                    r_map[rid]["name"]
                    for mm, rid in valid_locks
                    if mm == m_id and inst_by_r.get(rid, "") == inst
                ]
                warnings.append({
                    "code": "institution_lock_over_cap",
                    "message": f"《{m_map[m_id]['title']}》来自「{inst}」的锁定评审已有 {cnt} 人，"
                               f"超过每篇同机构最多 {org_cap} 人的限制"
                               f"（{('、'.join(over_names))}）；锁定分配强制保留，本次不再安排该机构的其他评审人。",
                    "manuscript_id": m_id, "institution": inst,
                    "locked_count": cnt, "cap": org_cap,
                })

    # ------------------------------------------------- 锁定部分的费用（确定性）
    def uncovered(m_id: int, r_id: int) -> int:
        return len(set(m_by_id[m_id]["topics"]) - set(r_by_id[r_id]["topics"]))

    edge_costs: dict[str, dict] = {}
    locked_cost = 0
    lock_slot_seen: dict[int, int] = {}
    for m_id, r_id in valid_locks:
        k = lock_slot_seen.get(r_id, 0) + 1
        lock_slot_seen[r_id] = k
        mm = beta * uncovered(m_id, r_id)
        ll = alpha * k
        locked_cost += mm + ll
        edge_costs[f"{m_id}:{r_id}"] = {
            "mismatch_cost": mm, "load_cost": ll,
            "covered": sorted(set(m_by_id[m_id]["topics"]) & set(r_by_id[r_id]["topics"])),
            "uncovered_topics": sorted(set(m_by_id[m_id]["topics"]) - set(r_by_id[r_id]["topics"])),
            "is_chair": (m_id, r_id) in locked_chairs,
        }

    locked_by_m_count: dict[int, int] = {}
    for m_id, _ in valid_locks:
        locked_by_m_count[m_id] = locked_by_m_count.get(m_id, 0) + 1

    # ------------------------------------------------------------ 构造流网络
    S = 0
    chair_base = 1
    chair_node = {m["id"]: chair_base + i for i, m in enumerate(manuscripts)}
    normal_base = chair_base + len(manuscripts)
    normal_node = {m["id"]: normal_base + i for i, m in enumerate(manuscripts)}
    r_base = normal_base + len(manuscripts)
    r_node = {r["id"]: r_base + i for i, r in enumerate(reviewers)}

    # 可建“分配边”的（稿件, 评审人）：冲突边绝不加入；锁定边已预占，也不重复建立。
    eligible_pairs: list[tuple[int, int]] = []
    for m in manuscripts:
        for r in reviewers:
            m_id, r_id = m["id"], r["id"]
            if (m_id, r_id) in conflict_pairs or (m_id, r_id) in locked_pairs:
                continue
            eligible_pairs.append((m_id, r_id))

    # 机构路径：通道节点 -> 通道匹配 P^lane_{m,a} -> 共享配额 Q_{m,a} -> R_j。
    # 仅当开启机构上限（org_cap>0）、评审人填有机构、且该稿该机构历史锁定未超限时建立。
    org_keys: set[tuple[int, str]] = set()
    if org_cap > 0:
        for m_id, r_id in eligible_pairs:
            inst = inst_by_r[r_id]
            if inst and (m_id, inst) not in inst_over:
                org_keys.add((m_id, inst))
    org_list = sorted(org_keys)
    p_base = r_base + len(reviewers)
    p_chair_node = {key: p_base + 2 * i for i, key in enumerate(org_list)}
    p_normal_node = {key: p_base + 2 * i + 1 for i, key in enumerate(org_list)}
    q_base = p_base + 2 * len(org_list)
    q_node = {key: q_base + i for i, key in enumerate(org_list)}      # 配额入口
    u_base = q_base + len(org_list)
    u_node = {key: u_base + i for i, key in enumerate(org_list)}      # 共享出口
    T = u_base + len(org_list)
    net = MinCostMaxFlow(T + 1)

    total_required = sum(m["required_reviews"] for m in manuscripts)
    total_chairs_required = sum(chairs_req.get(m["id"], 0) for m in manuscripts)
    total_capacity = sum(r["capacity"] for r in reviewers)

    # 主审优先惩罚 delta：大于任意“一个单位流”的原始费用上界。
    # 于是主审通道（无 delta）在最短路中总是先于普通通道（有 delta）被增广，
    # 保证：最大可行分配优先；总分配数相同时主审补齐数最大。
    max_topic_cost = max(
        (beta * max(1, len(m_by_id[m_id]["topics"])) for m_id, _ in eligible_pairs),
        default=beta,
    )
    max_slot = max((alpha * max(1, r["capacity"]) for r in reviewers), default=alpha)
    delta = max(1, max_topic_cost + max_slot + 1)

    free_chair_need: dict[int, int] = {}
    for m in manuscripts:
        m_id = m["id"]
        need = max(0, m["required_reviews"] - locked_by_m_count.get(m_id, 0))
        chair_need = min(
            need,
            max(0, chairs_req.get(m_id, 0) - locked_chair_count_by_m.get(m_id, 0)),
        )
        free_chair_need[m_id] = chair_need
        # 主审通道 S -> C_i：容量 = 尚缺主审数（只有合资格评审人的主审边接入）。
        if chair_need:
            net.add_edge(S, chair_node[m_id], chair_need, 0,
                         {"type": "source", "lane": "chair", "m_id": m_id, "cap": chair_need})
            # 回流边 C_i -> N_i：当合资格主审实在不足时，主审通道的剩余容量
            # 可转为普通名额以最大化总分配数（费用 2δ，迟于一切正常增广）。
            net.add_edge(chair_node[m_id], normal_node[m_id], chair_need, 2 * delta,
                         {"type": "chair_fallback", "m_id": m_id, "cap": chair_need})
        # 普通通道 S -> N_i：容量 = 尚缺总名额 - 尚缺主审数。
        # 主审通道 chair_need 与普通通道 (need-chair_need) 容量之和恰为 need，
        # 两条通道汇入同一组“每对容量 1”的分配边与同一组评审负载槽位后，
        # 同一人对同一稿至多 1 次、总分配数不超过需求；主审与普通评审共享
        # 个人容量与机构名额。主审通道只接合资格评审人且边费少 delta，
        # 故主审名额在最大可行分配前提下总是优先由合资格评审人补齐。
        if need - chair_need:
            net.add_edge(S, normal_node[m_id], need - chair_need, 0,
                         {"type": "source", "lane": "normal", "m_id": m_id,
                          "cap": need - chair_need})

    # 通道匹配 P^lane -> 配额入口 Q（无费、容量足够大）；Q -> U 为全稿唯一的
    # 机构配额边（容量 = 机构上限 - 该稿该机构锁定占用）。主审/普通两通道都经
    # Q -> U -> 评审，因此机构名额在两通道间严格共享，主审同样占用机构名额。
    for (m_id, inst) in org_list:
        cap = max(0, org_cap - locked_inst_count.get((m_id, inst), 0))
        net.add_edge(p_chair_node[(m_id, inst)], q_node[(m_id, inst)], cap, 0, None)
        net.add_edge(p_normal_node[(m_id, inst)], q_node[(m_id, inst)], cap, 0, None)
        if cap > 0:
            net.add_edge(
                q_node[(m_id, inst)], u_node[(m_id, inst)], cap, 0,
                {"type": "institution", "m_id": m_id, "institution": inst, "cap": cap},
            )

    for m_id, r_id in eligible_pairs:
        inst = inst_by_r[r_id]
        base_cost = beta * uncovered(m_id, r_id)
        qualified = can_chair_by_r.get(r_id, False) and free_chair_need[m_id] > 0

        if org_cap > 0 and inst:
            # 历史锁定已超限的机构：无 Q/U 节点 -> 不再分配该机构任何人。
            u = u_node.get((m_id, inst))
            if u is None:
                continue
            # 通道 -> 各自匹配节点（零费）-> 共享配额 Q->U -> 评审。
            # U -> R 的分配边每对（稿件,评审人）只建一条，结构上保证同人同稿唯一。
            net.add_edge(normal_node[m_id], p_normal_node[(m_id, inst)], 1, 0, None)
            if qualified:
                net.add_edge(chair_node[m_id], p_chair_node[(m_id, inst)], 1, 0, None)
            net.add_edge(
                u, r_node[r_id], 1, base_cost,
                {"type": "assign", "m_id": m_id, "r_id": r_id, "cap": 1})
        else:
            # 机构上限关闭、或评审人未填写机构（不占用机构名额）：通道直接建边。
            net.add_edge(
                normal_node[m_id], r_node[r_id], 1, base_cost + delta,
                {"type": "assign", "m_id": m_id, "r_id": r_id, "lane": "normal", "cap": 1})
            if qualified:
                net.add_edge(
                    chair_node[m_id], r_node[r_id], 1, base_cost,
                    {"type": "assign", "m_id": m_id, "r_id": r_id, "lane": "chair", "cap": 1})

    for r in reviewers:
        r_id = r["id"]
        lc = locked_count.get(r_id, 0)
        for k in range(1, free_capacity[r_id] + 1):
            slot = lc + k  # 全局负载槽位序号，与锁定部分连续计费
            net.add_edge(
                r_node[r_id], T, 1, alpha * slot,
                {"type": "slot", "r_id": r_id, "slot": slot, "cap": 1},
            )

    # ------------------------------------------------------------- 求解与落库
    _, raw_free_cost = net.solve(S, T)

    free_pairs: list[tuple[int, int]] = []
    # 主审源点 S->C 的实际流量（含随后经回流边转走的部分）
    chair_src_by_m: dict[int, int] = {}
    fallback_by_m: dict[int, int] = {}  # 每稿经 C_i->N_i 回流为普通名额的流量
    fallback_units = 0     # 回流总流量
    normal_lane_edges = 0  # 无机构路径上“普通分配边”承载的流量（边费含 delta）
    for tag, used in net.used_tags():
        if tag["type"] == "assign" and used > 0:
            free_pairs.append((tag["m_id"], tag["r_id"]))
            if tag.get("lane") == "normal":
                normal_lane_edges += used
        elif tag["type"] == "source" and tag["lane"] == "chair" and used > 0:
            chair_src_by_m[tag["m_id"]] = used
        elif tag["type"] == "chair_fallback" and used > 0:
            fallback_by_m[tag["m_id"]] = fallback_by_m.get(tag["m_id"], 0) + used
            fallback_units += used
    free_pairs.sort()
    # 真正经主审通道补到评审人的主审数 = 主审源点流量 - 回流到普通通道的流量。
    # （主审通道只接合资格评审人，且同一（稿件,评审人）在结构上至多 1 单位。）
    chair_flow_by_m: dict[int, int] = {
        m_id: chair_src_by_m.get(m_id, 0) - fallback_by_m.get(m_id, 0)
        for m_id in set(chair_src_by_m) | set(fallback_by_m)
    }
    # 还原真实费用（与未开启主审约束时口径一致）：
    # 普通直接边多收了 delta；回流边收了 2delta；机构路径的普通名额不额外收费。
    free_cost = (raw_free_cost
                 - normal_lane_edges * delta
                 - fallback_units * 2 * delta)

    # 主审通道流量只表明“每稿补齐了几名主审”；具体由哪位合资格评审人出任主审，
    # 按评审人 ID 升序（与锁定抵扣口径一致）确定性选取，保证结果可复现、可展示。
    free_chair_pairs: set[tuple[int, int]] = set()
    free_pairs_by_m: dict[int, list[int]] = {}
    for m_id, r_id in free_pairs:
        free_pairs_by_m.setdefault(m_id, []).append(r_id)
    for m_id, n_chairs in chair_flow_by_m.items():
        qualified = sorted(rid for rid in free_pairs_by_m.get(m_id, [])
                           if can_chair_by_r.get(rid, False))
        free_chair_pairs.update((m_id, rid) for rid in qualified[:n_chairs])

    # 自由分配边的费用拆解（槽位按评审人分组后顺序对应）
    assigned_slot: dict[int, list[int]] = {}
    for tag, used in net.used_tags():
        if tag["type"] == "slot" and used > 0:
            assigned_slot.setdefault(tag["r_id"], []).append(tag["slot"])
    ms_by_reviewer: dict[int, list[int]] = {}
    for m_id, r_id in free_pairs:
        ms_by_reviewer.setdefault(r_id, []).append(m_id)
    for r_id, ms in ms_by_reviewer.items():
        for idx, m_id in enumerate(sorted(ms)):
            slots = sorted(assigned_slot.get(r_id, []))
            slot = slots[idx] if idx < len(slots) else (locked_count.get(r_id, 0) + idx + 1)
            mm = beta * uncovered(m_id, r_id)
            ll = alpha * slot
            edge_costs[f"{m_id}:{r_id}"] = {
                "mismatch_cost": mm, "load_cost": ll,
                "covered": sorted(set(m_by_id[m_id]["topics"]) & set(r_by_id[r_id]["topics"])),
                "uncovered_topics": sorted(set(m_by_id[m_id]["topics"]) - set(r_by_id[r_id]["topics"])),
                "is_chair": (m_id, r_id) in free_chair_pairs,
            }

    # 事务内用新结果替换所有非锁定分配
    with transaction(conn):
        conn.execute("DELETE FROM assignments WHERE locked = 0")
        for m_id, r_id in free_pairs:
            conn.execute(
                "INSERT INTO assignments(m_id, r_id, locked) VALUES (?, ?, 0)",
                (m_id, r_id),
            )

    # --------------------------------------------------------- 未满足稿件分析
    assigned_by_m: dict[int, int] = {}
    for m_id, _ in valid_locks:
        assigned_by_m[m_id] = assigned_by_m.get(m_id, 0) + 1
    for m_id, _ in free_pairs:
        assigned_by_m[m_id] = assigned_by_m.get(m_id, 0) + 1

    # 实际主审（锁定主审 + 经主审通道补齐的自由分配）
    chair_pairs: set[tuple[int, int]] = locked_chairs | free_chair_pairs
    chairs_assigned_by_m: dict[int, int] = {}
    for m_id, _ in chair_pairs:
        chairs_assigned_by_m[m_id] = chairs_assigned_by_m.get(m_id, 0) + 1

    # 本稿已占用（锁定或新分配）的评审不能重复计入候选（每稿-每人最多一次）。
    assigned_pairs: set[tuple[int, int]] = locked_pairs | set(free_pairs)

    # 每篇稿件最终的机构占用（锁定 + 新分配；未填机构归入空串，不参与上限统计）。
    inst_used: dict[int, dict[str, int]] = {}
    for m_id, r_id in valid_locks:
        inst = inst_by_r.get(r_id, "")
        if inst:
            inst_used.setdefault(m_id, {})[inst] = inst_used.setdefault(m_id, {}).get(inst, 0) + 1
    for m_id, r_id in free_pairs:
        inst = inst_by_r.get(r_id, "")
        if inst:
            inst_used.setdefault(m_id, {})[inst] = inst_used.setdefault(m_id, {}).get(inst, 0) + 1

    unmet: list[dict] = []
    for m in manuscripts:
        m_id = m["id"]
        got = assigned_by_m.get(m_id, 0)
        deficit = m["required_reviews"] - got
        chair_need = chairs_req.get(m_id, 0)
        chair_got = chairs_assigned_by_m.get(m_id, 0)
        chair_deficit = max(0, chair_need - chair_got)
        # 总人数不足或主审不足均列入未满足（两类缺口分别给出）
        if deficit <= 0 and chair_deficit <= 0:
            continue

        reasons: list[str] = []
        candidates = []
        blocked_by_conflict = 0
        blocked_by_institution = 0
        chair_blocked_by_conflict = 0   # 具主审资格但与本稿冲突
        chair_blocked_by_institution = 0  # 具主审资格但被机构上限排除
        chair_blocked_by_capacity = 0   # 具主审资格但剩余容量为 0
        chair_blocked_assigned = 0      # 具主审资格但已在评审本稿（不可重复）
        chair_eligible_available = 0    # 具主审资格、当前可用于补主审（含容量）
        assigned_here = 0
        candidate_capacity = 0
        effective_capacity = 0
        inst_blocked_names: dict[str, int] = {}
        for r in reviewers:
            r_id = r["id"]
            is_chair_qualified = can_chair_by_r.get(r_id, False)
            if (m_id, r_id) in conflict_pairs:
                blocked_by_conflict += 1
                if is_chair_qualified:
                    chair_blocked_by_conflict += 1
                continue
            if (m_id, r_id) in assigned_pairs:
                assigned_here += 1  # 已计入 got（锁定或本次新分配），不能重复评审本稿
                if is_chair_qualified:
                    chair_blocked_assigned += 1
                continue
            rem = free_capacity[r_id]
            miss = uncovered(m_id, r_id)
            inst = inst_by_r.get(r_id, "")
            # 开启机构上限时：该稿该机构名额已满（含历史锁定超限）则该候选被硬性排除。
            inst_full = bool(
                org_cap > 0 and inst
                and ((m_id, inst) in inst_over or inst_used.get(m_id, {}).get(inst, 0) >= org_cap)
            )
            candidate_capacity += rem
            if inst_full:
                blocked_by_institution += 1
                inst_blocked_names[inst] = inst_blocked_names.get(inst, 0) + 1
                if is_chair_qualified:
                    chair_blocked_by_institution += 1
            if is_chair_qualified:
                if inst_full:
                    pass
                elif rem <= 0:
                    chair_blocked_by_capacity += 1
                else:
                    chair_eligible_available += rem
            if rem > 0 and not inst_full:
                effective_capacity += rem
            candidates.append({
                "reviewer_id": r_id, "name": r["name"],
                "institution": inst or None,
                "can_chair": is_chair_qualified,
                "remaining_capacity": rem,
                "uncovered_topics": miss,
                "topic_match": "full" if miss == 0 else "partial",
                "blocked_by_institution": inst_full,
                # 该候选能否补主审：资格、冲突、容量、机构四重条件同时满足
                "chair_available": bool(
                    is_chair_qualified and rem > 0 and not inst_full),
                "chair_block_reason": (
                    None if (is_chair_qualified and rem > 0 and not inst_full)
                    else ("not_qualified" if not is_chair_qualified
                          else ("no_capacity" if rem <= 0 else "institution"))),
            })

        # 主审缺口：总人数已满但主审不足（或两者皆缺）时单独说明主审阻断原因。
        qualified_n = sum(1 for r in reviewers if can_chair_by_r.get(r["id"], False))
        # 未被选作主审但“本可担任”的合资格评审（非冲突、未重复、未被机构阻断）
        unchosen_qualified = [
            c for c in candidates
            if c["can_chair"] and not c["blocked_by_institution"]]
        unchosen_cap0 = sum(1 for c in unchosen_qualified if c["remaining_capacity"] <= 0)
        unchosen_ready = [c for c in unchosen_qualified if c["remaining_capacity"] > 0]
        if chair_deficit > 0:
            chair_bits = [f"全库具备主审资格的评审人共 {qualified_n} 名"]
            if chair_blocked_by_conflict:
                chair_bits.append(f"{chair_blocked_by_conflict} 名与本稿存在作者冲突")
            if chair_blocked_assigned:
                chair_bits.append(f"{chair_blocked_assigned} 名已在评审本稿（不可重复，其中主审 {min(chair_got, chair_blocked_assigned)} 名）")
            if chair_blocked_by_institution:
                chair_bits.append(f"{chair_blocked_by_institution} 名被机构上限排除")
            if unchosen_cap0:
                chair_bits.append(f"{unchosen_cap0} 名剩余容量为 0")
            reasons.append(
                f"主审缺口 {chair_deficit} 名：本稿需 {chair_need} 名主审、实际 {chair_got} 名。"
                + "；".join(chair_bits) + "。"
                + (f"另有 {len(unchosen_ready)} 名合资格评审有剩余容量（合计 "
                   f"{sum(c['remaining_capacity'] for c in unchosen_ready)}），"
                   f"但这些容量与其他稿件共享，全局竞争后仍缺 {chair_deficit} 名主审。"
                   if unchosen_ready
                   else "没有同时满足资格、无冲突、有容量且不被机构名额阻断的评审人可补主审。")
                + "可在「评审人」中为主审候选勾选“可任主审”、提高其容量、解除冲突或放宽机构上限。"
            )

        # 以下为总人数缺口原因；总人数已满（仅主审不足）时无需再报。
        if deficit > 0:
            if blocked_by_conflict:
                reasons.append(f"{len(reviewers)} 名评审人中有 {blocked_by_conflict} 名因作者冲突被禁止评审本稿。")
            if blocked_by_institution:
                detail = "、".join(
                    f"「{inst}」{n} 名" for inst, n in sorted(inst_blocked_names.items()))
                has_over_lock = any((m_id, inst) in inst_over for inst in inst_blocked_names)
                if has_over_lock:
                    reasons.append(
                        f"另有 {blocked_by_institution} 名非冲突候选被机构上限排除（{detail}）："
                        f"该机构在本稿的锁定分配已超过每篇同机构最多 {org_cap} 人，锁定保留且不再安排同机构评审人。"
                    )
                else:
                    reasons.append(
                        f"另有 {blocked_by_institution} 名非冲突候选因机构多样性上限被排除（{detail}）："
                        f"本稿来自该机构的评审（含锁定）已达每篇同机构最多 {org_cap} 人。"
                    )
            # 注意：主题不匹配只是高费用（软约束），不禁止分配；
            # 这里分层展示，便于人工复核未满足的真正原因。
            effective_candidates = [c for c in candidates
                                    if c["remaining_capacity"] > 0 and not c["blocked_by_institution"]]
            compatible_cap = sum(c["remaining_capacity"] for c in effective_candidates
                                 if c["topic_match"] == "full")
            compatible_n = sum(1 for c in effective_candidates if c["topic_match"] == "full")
            any_remaining = any(c["remaining_capacity"] > 0 for c in candidates)
            if not candidates:
                # 非冲突评审都已在本稿（锁定或本次新分配），无人可再补充
                reasons.append(
                    f"无冲突评审人共 {assigned_here} 名，均已评审本稿，无法重复安排；"
                    f"可在「作者冲突」中解除冲突，或在「评审人」中增加相容评审。"
                )
            elif not any_remaining:
                reasons.append("其余评审人剩余容量均为 0（个人容量已被锁定或其他稿件占满）。")
            elif effective_capacity < deficit:
                reasons.append(
                    f"扣除冲突与机构上限后，可用评审人的剩余总容量为 {effective_capacity}，小于本稿缺口 {deficit}；"
                    f"其中主题完全相容者 {compatible_n} 名、剩余容量合计 {compatible_cap}。"
                    f"可在「评审人」中提高容量、解除相关冲突，或在设置中放宽每篇同机构人数上限。"
                )
            else:
                reasons.append(
                    f"扣除冲突与机构上限后，可用评审人剩余总容量为 {effective_capacity}（不小于缺口 {deficit}），"
                    f"但这些容量与其他稿件共享（每稿-每人最多一次），在全局最小费用竞争下本稿仍缺 {deficit}；"
                    f"主题完全相容者 {compatible_n} 名、剩余容量合计 {compatible_cap}，"
                    f"可考虑增加方向匹配的评审人或提高其容量。"
                )
            if total_capacity < total_required:
                reasons.append(
                    f"全局性容量不足：全部评审人容量之和 {total_capacity} < 全部稿件需求之和 {total_required}。"
                )

        unmet.append({
            "manuscript_id": m_id,
            "title": m["title"],
            "required": m["required_reviews"],
            "assigned": got,
            "deficit": deficit,
            "required_chairs": chair_need,
            "assigned_chairs": chair_got,
            "chair_deficit": chair_deficit,
            "reasons": reasons,
            "details": {
                "reviewer_count": len(reviewers),
                "blocked_by_conflict": blocked_by_conflict,
                "blocked_by_institution": blocked_by_institution,
                "assigned_here": assigned_here,
                "candidate_remaining_capacity": candidate_capacity,
                "effective_remaining_capacity": effective_capacity,
                "chair_qualified_count": qualified_n,
                "chair_blocked_by_conflict": chair_blocked_by_conflict,
                "chair_blocked_by_institution": chair_blocked_by_institution,
                "chair_blocked_by_capacity": chair_blocked_by_capacity,
                "chair_blocked_assigned": chair_blocked_assigned,
                "chair_available_capacity": chair_eligible_available,
                "candidates": candidates,
            },
        })

    # 每篇稿件的机构构成（用于分配结果展示；未填机构的评审人归入「未填写机构」）。
    composition: dict[int, list[dict]] = {}
    for m in manuscripts:
        m_id = m["id"]
        groups: dict[str, dict] = {}
        for mm, r_id in valid_locks:
            if mm != m_id:
                continue
            inst = inst_by_r.get(r_id, "") or ""
            g = groups.setdefault(inst, {"institution": inst or None, "count": 0,
                                        "locked": 0, "over_cap": False})
            g["count"] += 1
            g["locked"] += 1
        for mm, r_id in free_pairs:
            if mm != m_id:
                continue
            inst = inst_by_r.get(r_id, "") or ""
            g = groups.setdefault(inst, {"institution": inst or None, "count": 0,
                                        "locked": 0, "over_cap": False})
            g["count"] += 1
        if org_cap > 0:
            for inst, g in groups.items():
                if inst and g["count"] > org_cap:
                    g["over_cap"] = True
        ordered = sorted(groups.values(),
                         key=lambda g: (g["institution"] is None, g["institution"] or ""))
        composition[m_id] = ordered

    total_chairs_assigned = (
        sum(locked_chair_count_by_m.values()) + len(free_chair_pairs))
    solution = {
        "solved_at": datetime.now().isoformat(timespec="seconds"),
        "mismatch_weight": beta,
        "load_weight": alpha,
        "institution_cap": org_cap,
        "total_required": total_required,
        "total_assigned": len(valid_locks) + len(free_pairs),
        "total_capacity": total_capacity,
        "total_chairs_required": total_chairs_required,
        "total_chairs_assigned": total_chairs_assigned,
        # 实际主审分配（稿件 -> 评审人 ID 列表，按 ID 排序），供结果页标识
        "chairs": {
            m_id: sorted(r_id for mm, r_id in chair_pairs if mm == m_id)
            for m_id in {mm for mm, _ in chair_pairs}
        },
        "locked_count": len(valid_locks),
        "total_cost": locked_cost + free_cost,
        "locked_cost": locked_cost,
        "free_cost": free_cost,
        "fully_satisfied": len(unmet) == 0,
        "unmet": unmet,
        "warnings": warnings,
        "edge_costs": edge_costs,
        "institutions": composition,
    }

    set_setting(conn, "last_solution", solution)
    conn.commit()
    return solution
