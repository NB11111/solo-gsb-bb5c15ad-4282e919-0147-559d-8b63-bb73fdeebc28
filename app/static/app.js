/* 匿名评审分配系统 —— 原生 JS 前端（无框架、无构建） */
"use strict";

let state = null;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* --------------------------------------------------------------- API */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `请求失败（${res.status}）`);
  }
  return data;
}

async function refresh() {
  state = await api("/api/state");
  render();
}

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
}

async function runAction(promise, okMsg) {
  try {
    const data = await promise;
    state = data;
    render();
    if (okMsg) toast(okMsg);
    return data;
  } catch (e) {
    toast(e.message, true);
    throw e;
  }
}

/* --------------------------------------------------------------- 渲染：总览 */
function indexMaps() {
  state._mById = Object.fromEntries(state.manuscripts.map((m) => [m.id, m]));
  state._rById = Object.fromEntries(state.reviewers.map((r) => [r.id, r]));
  state._assignByM = {};
  for (const a of state.assignments) {
    (state._assignByM[a.manuscript_id] ||= []).push(a);
  }
  state._conflictSet = new Set(state.conflicts.map((c) => `${c.manuscript_id}:${c.reviewer_id}`));
  // 实际主审（稿件:评审人）集合，由最近一次求解结果给出
  state._leadSet = new Set((state.solution?.lead_pairs || []).map(([m, r]) => `${m}:${r}`));
}

function renderSummary() {
  const sol = state.solution;
  const cards = [];
  if (sol) {
    cards.push(
      stat(sol.total_assigned, `已满足评审数（共需 ${sol.total_required}）`,
        sol.total_assigned >= sol.total_required ? "ok" : "bad"),
      stat(sol.fully_satisfied ? "完整" : `${sol.unmet.length} 篇未满足`,
        "分配完整性", sol.fully_satisfied ? "ok" : "bad"),
      stat(`${sol.total_lead_assigned ?? 0} / ${sol.total_lead_required ?? 0}`, "主审已认定 / 共需",
        (sol.total_lead_assigned ?? 0) >= (sol.total_lead_required ?? 0) ? "ok" : "bad"),
      stat(sol.total_cost, "目标函数总费用（越小越优）"),
      stat(sol.locked_count, "锁定的既有分配"),
      stat(sol.total_capacity, "评审人总容量"),
    );
  } else {
    cards.push(stat(state.manuscripts.length, "稿件数"));
    cards.push(stat(state.reviewers.length, "评审人数"));
    cards.push(stat(state.assignments.filter((a) => a.locked).length, "锁定分配"));
  }
  $("#summary-cards").innerHTML = cards.join("");
}

function stat(num, label, cls = "") {
  return `<div class="stat ${cls}"><div class="num">${esc(num)}</div><div class="label">${esc(label)}</div></div>`;
}

function renderAlerts() {
  const sol = state.solution;
  if (!sol) {
    $("#warnings-box").innerHTML = "";
    $("#unmet-box").innerHTML =
      `<div class="alert warn">尚未求解，点击右上角「重新求解分配」生成完整分配。</div>`;
    return;
  }

  $("#warnings-box").innerHTML = sol.warnings.length
    ? `<div class="alert warn"><strong>⚠️ 求解警告（${sol.warnings.length}）</strong><ul>` +
      sol.warnings.map((w) => `<li>${esc(w.message)}</li>`).join("") + `</ul></div>`
    : (sol.fully_satisfied
      ? `<div class="alert success">✅ 全部 ${sol.total_required} 个评审需求均已满足，且未使用任何冲突边。</div>`
      : "");

  if (!sol.unmet.length) {
    $("#unmet-box").innerHTML = "";
    return;
  }

  $("#unmet-box").innerHTML = sol.unmet.map((u) => {
    const detailRows = u.details.candidates
      .map((c) => `<tr class="${c.blocked_by_institution ? "row-blocked" : ""}">
          <td>${esc(c.name)}</td>
          <td>${c.can_lead
            ? '<span class="tag lead-tag">⭐ 可任主审</span>'
            : '<span class="hint">—</span>'}</td>
          <td>${c.institution ? `🏛 ${esc(c.institution)}` : '<span class="hint">未填写</span>'}</td>
          <td>${c.remaining_capacity > 0
            ? `剩余容量 ${c.remaining_capacity}`
            : '<span style="color:#c63b3b">容量已满</span>'}</td>
          <td>${c.uncovered_topics === 0
            ? '<span style="color:#178a52">主题全覆盖</span>'
            : `未覆盖主题 ${c.uncovered_topics} 个`}</td>
          <td>${c.blocked_by_institution
            ? '<span class="excl">被机构上限排除</span>' : ""}</td>
        </tr>`).join("");
    const instBlocked = u.details.blocked_by_institution || 0;
    const headParts = [];
    if (u.deficit > 0) headParts.push(`需要 ${u.required} 名评审，仅安排 ${u.assigned} 名，缺 ${u.deficit} 名`);
    if (u.lead_deficit > 0) headParts.push(`需主审 ${u.lead_required} 名，实际 ${u.lead_assigned} 名，主审缺口 ${u.lead_deficit} 名`);
    return `<div class="alert danger">
      <strong>⛔ 《${esc(u.title)}》未满足：${headParts.join("；")}。</strong>
      <ul>${u.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      <div class="reason-detail">
        <strong>可复核明细</strong>（共 ${u.details.reviewer_count} 名评审，
        冲突排除 ${u.details.blocked_by_conflict} 名${
          u.details.assigned_here ? `，已评审本稿 ${u.details.assigned_here} 名（不可重复）` : ""}${
          instBlocked ? `，机构上限排除 ${instBlocked} 名` : ""}，
        其余剩余容量合计 ${u.details.candidate_remaining_capacity}${
          instBlocked ? `；扣除机构上限后实际可用 ${u.details.effective_remaining_capacity}` : ""}${
          u.lead_required > 0 ? `；可任主审者 ${u.details.lead_pool} 名、当前可补充主审 ${u.details.lead_available} 名` : ""}）
        <table>
          <tr><th>评审人</th><th>主审资格</th><th>机构</th><th>剩余容量</th><th>主题匹配</th><th>排除原因</th></tr>
          ${detailRows}
        </table>
      </div>
    </div>`;
  }).join("");
}

function costInfo(mId, rId) {
  return state.solution?.edge_costs?.[`${mId}:${rId}`] || null;
}

function renderAssignments() {
  if (!state.manuscripts.length) {
    $("#assignment-list").innerHTML = `<div class="empty">暂无稿件，请先到「稿件」页录入。</div>`;
    return;
  }
  $("#assignment-list").innerHTML = state.manuscripts.map((m) => {
    const assigns = state._assignByM[m.id] || [];
    const short = assigns.length < m.required_reviews;
    const leadGot = assigns.filter((a) => state._leadSet.has(`${m.id}:${a.reviewer_id}`)).length;
    const leadShort = (m.required_leads || 0) > leadGot;
    const chips = assigns
      .slice()
      .sort((a, b) => (b.locked - a.locked) || a.reviewer_id - b.reviewer_id)
      .map((a) => {
        const r = state._rById[a.reviewer_id];
        const isLead = state._leadSet.has(`${m.id}:${a.reviewer_id}`);
        const ci = costInfo(m.id, a.reviewer_id);
        const miss = ci && ci.uncovered_topics.length
          ? `<span class="tag miss">未覆盖：${ci.uncovered_topics.map(esc).join("、")}</span>`
          : "";
        const instTxt = r && r.institution ? `<span class="tag inst">🏛 ${esc(r.institution)}</span>` : "";
        const costTxt = ci ? `费用 ${ci.mismatch_cost + ci.load_cost}` : "锁定";
        return `<span class="chip ${a.locked ? "locked" : ""} ${isLead ? "lead" : ""} ${ci && ci.uncovered_topics.length ? "miss-penalty" : ""}">
          ${a.locked ? "🔒" : "👤"} ${esc(r ? r.name : `#${a.reviewer_id}`)}
          ${isLead ? '<span class="tag lead-tag" title="实际主审">⭐ 主审</span>' : ""}
          ${instTxt}
          <span class="cost">${costTxt}</span>
          ${miss}
          <button title="${a.locked ? "解锁并重新求解" : "锁定该既有分配并重新求解"}"
            data-lock="${m.id}" data-reviewer="${a.reviewer_id}"
            data-locked="${a.locked ? "0" : "1"}">${a.locked ? "🔓" : "🔒"}</button>
        </span>`;
      }).join("");

    // 可手工锁定的候选：尚未分配、无冲突
    const usedIds = new Set(assigns.map((a) => a.reviewer_id));
    const options = state.reviewers
      .filter((r) => !usedIds.has(r.id) && !state._conflictSet.has(`${m.id}:${r.id}`))
      .map((r) => `<option value="${r.id}">${esc(r.name)}（${r.institution ? esc(r.institution) + " · " : ""}剩余 ${Math.max(0, r.capacity - r.assigned)}）</option>`)
      .join("");
    const manualLock = options
      ? `<div class="manual-lock">
          <span class="hint">手工锁定既有分配：</span>
          <select id="lock-select-${m.id}"><option value="">选择评审人…</option>${options}</select>
          <button class="btn small" data-manual-lock="${m.id}">🔒 锁定并重算</button>
        </div>`
      : "";

    return `<div class="assignment-card ${short || leadShort ? "short" : "satisfied"}">
      <div class="assignment-head">
        <div>
          <div class="assignment-title">《${esc(m.title)}》</div>
          <div class="entity-sub">主题：${m.topics.map((t) => `<span class="tag">${esc(t)}</span>`).join("") || "（未标注）"}</div>
        </div>
        <div>
          <span class="badge-count ${short ? "no" : "ok"}">${assigns.length} / ${m.required_reviews} 名评审</span>
          ${(m.required_leads || 0) > 0
            ? `<span class="badge-count ${leadShort ? "no" : "ok"}" title="实际主审 / 需主审">⭐ ${leadGot} / ${m.required_leads} 主审</span>`
            : ""}
        </div>
      </div>
      <div class="reviewer-chips">${chips || '<span class="hint">尚未分配</span>'}</div>
      ${compositionLine(m.id, assigns)}
      ${manualLock}
    </div>`;
  }).join("");
}

function compositionLine(mId, assigns) {
  const cap = state.settings.institution_cap || 0;
  const counts = new Map(); // institution -> {count, locked}
  for (const a of assigns) {
    const inst = (state._rById[a.reviewer_id] || {}).institution || "";
    const g = counts.get(inst) || { count: 0, locked: 0 };
    g.count += 1;
    if (a.locked) g.locked += 1;
    counts.set(inst, g);
  }
  const entries = [...counts.entries()].sort((x, y) => {
    if (!x[0] && !y[0]) return 0;
    if (!x[0]) return 1;
    if (!y[0]) return -1;
    return x[0].localeCompare(y[0], "zh");
  });
  const chipsHtml = entries.map(([inst, g]) => {
    const over = cap > 0 && inst && g.count > cap;
    const lockTxt = g.locked ? ` · 锁 ${g.locked}` : "";
    return `<span class="inst-chip ${over ? "over" : ""}" title="${over ? "历史锁定已超过每篇同机构人数上限" : ""}">${
      inst ? `🏛 ${esc(inst)}` : "未填写机构"} × ${g.count}${lockTxt}${over ? " ⚠️" : ""}</span>`;
  }).join("");
  const capTxt = cap > 0
    ? `<span class="hint">机构多样性上限：每篇同机构最多 ${cap} 人</span>`
    : `<span class="hint">机构多样性约束已关闭（设置中填 &gt;0 的上限可启用）</span>`;
  return `<div class="composition">
    <span class="hint">机构构成：</span>${chipsHtml || '<span class="hint">—</span>'} ${capTxt}
  </div>`;
}

function renderReviewerLoad() {
  if (!state.reviewers.length) {
    $("#reviewer-load").innerHTML = `<div class="empty">暂无评审人。</div>`;
    return;
  }
  $("#reviewer-load").innerHTML = state.reviewers.map((r) => {
    const load = r.assigned || 0;
    const pct = r.capacity > 0 ? Math.min(100, Math.round((load / r.capacity) * 100)) : (load > 0 ? 100 : 0);
    const over = load > r.capacity;
    return `<div class="load-row">
      <div class="load-name">${esc(r.name)}
        <div class="entity-sub" style="width:130px">${
          (r.institution ? `🏛 ${esc(r.institution)}` : "") || r.topics.slice(0, 2).map(esc).join(" / ")}</div>
      </div>
      <div class="load-bar-wrap ${over ? "over" : ""}">
        <div class="load-bar ${over ? "over" : ""}" style="width:${pct}%"></div>
      </div>
      <div class="load-text" style="color:${over ? "var(--red)" : ""}">
        ${load} / ${r.capacity}${r.locked_assigned ? `（锁 ${r.locked_assigned}）` : ""}
      </div>
    </div>`;
  }).join("");
}

/* --------------------------------------------------------------- 渲染：稿件/评审人 */
function renderManuscripts() {
  if (!state.manuscripts.length) {
    $("#manuscript-list").innerHTML = `<div class="empty">还没有稿件，使用上方表单录入。</div>`;
    return;
  }
  $("#manuscript-list").innerHTML = state.manuscripts.map((m) => `
    <div class="entity-row" data-mrow="${m.id}">
      <div class="entity-main">
        <div class="entity-title">《${esc(m.title)}》</div>
        <div class="entity-sub">
          需求 ${m.required_reviews} 名评审${m.required_leads ? `（含主审 ${m.required_leads} 名）` : ""} · 已安排 ${m.assigned || 0} ·
          主题：${m.topics.map((t) => `<span class="tag">${esc(t)}</span>`).join("") || "（未标注）"}
        </div>
      </div>
      <div class="entity-actions">
        <button class="btn small" data-edit-m="${m.id}">编辑</button>
        <button class="btn small danger-ghost" data-del-m="${m.id}">删除</button>
      </div>
    </div>`).join("");

  // 表单默认需求
  const defReq = state.settings.default_required_reviews;
  const reqInput = $('form#form-manuscript [name="required_reviews"]');
  if (reqInput && !reqInput.dataset.touched) reqInput.placeholder = `所需评审数（默认 ${defReq}）`;
  const leadsInput = $('form#form-manuscript [name="required_leads"]');
  if (leadsInput && !leadsInput.dataset.touched) leadsInput.placeholder = "需主审人数 0-2（默认 0）";
}

function renderReviewers() {
  if (!state.reviewers.length) {
    $("#reviewer-list").innerHTML = `<div class="empty">还没有评审人，使用上方表单录入。</div>`;
    return;
  }
  $("#reviewer-list").innerHTML = state.reviewers.map((r) => `
    <div class="entity-row">
      <div class="entity-main">
        <div class="entity-title">${esc(r.name)}
          ${r.can_lead ? '<span class="tag lead-tag">⭐ 可任主审</span>' : ""}
          ${r.locked_assigned ? '<span class="tag locked-tag">🔒 ' + r.locked_assigned + ' 锁定</span>' : ""}
        </div>
        <div class="entity-sub">
          容量 ${r.capacity} · 已安排 ${r.assigned || 0}${r.assigned > r.capacity ? " <b style='color:var(--red)'>(超容)</b>" : ""} ·
          机构：${r.institution ? `<span class="tag inst">${esc(r.institution)}</span>` : "（未填写，不计入机构上限）"} ·
          擅长：${r.topics.map((t) => `<span class="tag">${esc(t)}</span>`).join("") || "（未标注）"}
        </div>
      </div>
      <div class="entity-actions">
        <button class="btn small" data-edit-r="${r.id}">编辑</button>
        <button class="btn small danger-ghost" data-del-r="${r.id}">删除</button>
      </div>
    </div>`).join("");

  const defCap = state.settings.default_capacity;
  const capInput = $('form#form-reviewer [name="capacity"]');
  if (capInput && !capInput.dataset.touched) capInput.placeholder = `个人容量（默认 ${defCap}）`;
}

/* --------------------------------------------------------------- 渲染：冲突矩阵 */
function renderConflicts() {
  if (!state.manuscripts.length || !state.reviewers.length) {
    $("#conflict-matrix").innerHTML =
      `<div class="empty">需要至少一篇稿件和一名评审人才能编辑冲突。</div>`;
    return;
  }
  const head = `<tr><th>稿件 ＼ 评审人</th>${
    state.reviewers.map((r) => `<th>${esc(r.name)}</th>`).join("")}</tr>`;
  const rows = state.manuscripts.map((m) => `<tr>
    <td class="mtitle">《${esc(m.title)}》</td>
    ${state.reviewers.map((r) => {
      const checked = state._conflictSet.has(`${m.id}:${r.id}`);
      const locked = state.assignments.some(
        (a) => a.manuscript_id === m.id && a.reviewer_id === r.id && a.locked);
      return `<td><input type="checkbox" data-conflict="${m.id}" data-reviewer="${r.id}"
        ${checked ? "checked" : ""} ${locked ? "disabled" : ""}
        title="${locked ? "该分配已锁定，请先解锁" : "勾选表示存在作者冲突"}"></td>`;
    }).join("")}
  </tr>`).join("");
  $("#conflict-matrix").innerHTML = `<table class="matrix">${head}${rows}</table>`;
}

/* --------------------------------------------------------------- 设置 */
function renderSettings() {
  $("#set-default-req").value = state.settings.default_required_reviews;
  $("#set-default-cap").value = state.settings.default_capacity;
  $("#set-beta").value = state.settings.mismatch_weight;
  $("#set-alpha").value = state.settings.load_weight;
  $("#set-inst-cap").value = state.settings.institution_cap ?? 0;
}

function render() {
  indexMaps();
  renderSummary();
  renderAlerts();
  renderAssignments();
  renderReviewerLoad();
  renderManuscripts();
  renderReviewers();
  renderConflicts();
  renderSettings();
}

/* --------------------------------------------------------------- 通用编辑弹窗 */
function openModal(title, fields, onOk) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML =
    `<div class="modal-body-row">${fields.map((f) => f.type === "checkbox" ? `
      <label class="checkbox-label">${esc(f.label)}
        <input type="checkbox" id="mf-${f.key}" ${f.value ? "checked" : ""} />
      </label>` : `
      <label>${esc(f.label)}
        <input type="${f.type || "text"}" id="mf-${f.key}" value="${esc(f.value ?? "")}"
          min="${f.min ?? ""}" max="${f.max ?? ""}" />
      </label>`).join("")}</div>`;
  $("#modal").classList.remove("hidden");

  const cleanup = () => {
    $("#modal").classList.add("hidden");
    $("#modal-ok").onclick = null;
    $("#modal-cancel").onclick = null;
  };
  $("#modal-cancel").onclick = cleanup;
  $("#modal-ok").onclick = async () => {
    const values = {};
    for (const f of fields) {
      const el = $(`#mf-${f.key}`);
      values[f.key] = f.type === "checkbox" ? el.checked
        : f.type === "number" ? Number(el.value) : el.value;
    }
    try {
      await onOk(values);
      cleanup();
    } catch (e) {
      toast(e.message, true);
    }
  };
}

/* --------------------------------------------------------------- 事件绑定 */
function bindEvents() {
  // 标签页
  $("#tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $$(".tab-panel").forEach((p) =>
      p.classList.toggle("active", p.id === `tab-${tab.dataset.tab}`));
  });

  // 顶栏操作
  $("#btn-solve").addEventListener("click", async () => {
    const btn = $("#btn-solve");
    btn.disabled = true;
    try {
      await runAction(api("/api/solve", { method: "POST" }), "已用最小费用最大流完成分配");
    } catch (e) { /* toast handled */ } finally {
      btn.disabled = false;
    }
  });
  $("#btn-demo").addEventListener("click", async () => {
    if (!confirm("载入演示数据将覆盖现有稿件/评审人/冲突/分配，是否继续？")) return;
    await runAction(api("/api/demo", { method: "POST" }), "已载入演示数据并完成求解");
  });
  $("#btn-reset").addEventListener("click", async () => {
    if (!confirm("将清空所有稿件、评审人、冲突与分配，是否继续？")) return;
    await runAction(api("/api/reset", { method: "POST" }), "已清空");
  });

  // 新增稿件（required_reviews / required_leads 留空时用默认值）
  $("#form-manuscript").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const req = f.required_reviews.value.trim();
    const leads = f.required_leads.value.trim();
    await runAction(api("/api/manuscripts", {
      method: "POST",
      body: JSON.stringify({
        title: f.title.value,
        topics: f.topics.value,
        required_reviews: req ? Number(req) : state.settings.default_required_reviews,
        required_leads: leads ? Number(leads) : 0,
      }),
    }), "稿件已添加");
    f.reset();
  });

  // 新增评审人
  $("#form-reviewer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const cap = f.capacity.value.trim();
    await runAction(api("/api/reviewers", {
      method: "POST",
      body: JSON.stringify({
        name: f.name.value,
        topics: f.topics.value,
        institution: f.institution.value,
        capacity: cap ? Number(cap) : state.settings.default_capacity,
        can_lead: f.can_lead.checked,
      }),
    }), "评审人已添加");
    f.reset();
  });

  // 列表中的编辑/删除（事件委托）
  document.body.addEventListener("click", async (e) => {
    const editM = e.target.closest("[data-edit-m]");
    const delM = e.target.closest("[data-del-m]");
    const editR = e.target.closest("[data-edit-r]");
    const delR = e.target.closest("[data-del-r]");
    const lockBtn = e.target.closest("[data-lock]");
    const manualBtn = e.target.closest("[data-manual-lock]");

    if (editM) {
      const m = state._mById[editM.dataset.editM];
      openModal("编辑稿件", [
        { key: "title", label: "稿件主题/标题", value: m.title },
        { key: "topics", label: "主题（逗号分隔）", value: m.topics.join(", ") },
        { key: "required_reviews", label: "所需评审数", value: m.required_reviews, type: "number", min: 1, max: 20 },
        { key: "required_leads", label: "需主审人数（0-2，不超过所需评审数）", value: m.required_leads ?? 0, type: "number", min: 0, max: 2 },
      ], async (v) => {
        if (!v.title.trim()) throw new Error("标题不能为空");
        await runAction(api(`/api/manuscripts/${m.id}`, {
          method: "PATCH",
          body: JSON.stringify(v),
        }), "稿件已更新");
      });
    } else if (delM) {
      const m = state._mById[delM.dataset.delM];
      if (!confirm(`确定删除稿件《${m.title}》？相关分配将一并删除。`)) return;
      await runAction(api(`/api/manuscripts/${m.id}`, { method: "DELETE" }), "稿件已删除");
    } else if (editR) {
      const r = state._rById[editR.dataset.editR];
      openModal("编辑评审人", [
        { key: "name", label: "姓名/代号", value: r.name },
        { key: "topics", label: "擅长主题（逗号分隔）", value: r.topics.join(", ") },
        { key: "institution", label: "所属机构（可选，留空不计入机构上限）", value: r.institution || "" },
        { key: "capacity", label: "个人容量", value: r.capacity, type: "number", min: 0, max: 50 },
        { key: "can_lead", label: "可任主审（主审资格硬约束）", value: !!r.can_lead, type: "checkbox" },
      ], async (v) => {
        if (!v.name.trim()) throw new Error("姓名不能为空");
        await runAction(api(`/api/reviewers/${r.id}`, {
          method: "PATCH",
          body: JSON.stringify(v),
        }), "评审人已更新");
      });
    } else if (delR) {
      const r = state._rById[delR.dataset.delR];
      if (!confirm(`确定删除评审人 ${r.name}？相关分配将一并删除。`)) return;
      await runAction(api(`/api/reviewers/${r.id}`, { method: "DELETE" }), "评审人已删除");
    } else if (lockBtn) {
      const mId = Number(lockBtn.dataset.lock);
      const rId = Number(lockBtn.dataset.reviewer);
      const toLocked = lockBtn.dataset.locked === "1";
      if (toLocked && !confirm("锁定该既有分配？锁定后它将占用容量并参与负载均衡，随后自动重新求解。")) return;
      await runAction(api("/api/assignments/lock", {
        method: "POST",
        body: JSON.stringify({ manuscript_id: mId, reviewer_id: rId, locked: toLocked }),
      }), toLocked ? "已锁定并重新求解" : "已解锁并重新求解");
    } else if (manualBtn) {
      const mId = Number(manualBtn.dataset.manualLock);
      const sel = $(`#lock-select-${mId}`);
      if (!sel.value) return toast("请先选择一名评审人", true);
      await runAction(api("/api/assignments/lock", {
        method: "POST",
        body: JSON.stringify({
          manuscript_id: mId, reviewer_id: Number(sel.value), locked: true,
        }),
      }), "已锁定该既有分配并重新求解");
    }
  });

  // 冲突矩阵
  $("#conflict-matrix").addEventListener("change", async (e) => {
    const cb = e.target.closest("input[data-conflict]");
    if (!cb) return;
    const mId = Number(cb.dataset.conflict);
    const rId = Number(cb.dataset.reviewer);
    try {
      if (cb.checked) {
        await runAction(api("/api/conflicts", {
          method: "POST",
          body: JSON.stringify({ manuscript_id: mId, reviewer_id: rId }),
        }), "已登记冲突");
      } else {
        await runAction(api(`/api/conflicts/${mId}/${rId}`, { method: "DELETE" }), "已移除冲突");
      }
    } catch (err) {
      cb.checked = !cb.checked; // 回滚 UI
    }
  });

  // 设置保存
  $("#btn-save-settings").addEventListener("click", async () => {
    await runAction(api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        default_required_reviews: Number($("#set-default-req").value),
        default_capacity: Number($("#set-default-cap").value),
        mismatch_weight: Number($("#set-beta").value),
        load_weight: Number($("#set-alpha").value),
        institution_cap: Number($("#set-inst-cap").value),
      }),
    }), "设置已保存（点击“重新求解分配”可按新权重/机构上限计算）");
  });

  // 记录用户是否手动改过默认输入框
  $$('form input[name="required_reviews"], form input[name="capacity"]').forEach((i) =>
    i.addEventListener("input", () => (i.dataset.touched = "1")));
}

/* --------------------------------------------------------------- 启动 */
(async function init() {
  bindEvents();
  try {
    state = await api("/api/state");
    indexMaps();
    render();
    // 首次进入（如全新数据库）自动求解一次
    if (!state.solution && state.manuscripts.length) {
      state = await api("/api/solve", { method: "POST" });
      render();
    }
  } catch (e) {
    toast("初始化失败：" + e.message, true);
  }
})();
