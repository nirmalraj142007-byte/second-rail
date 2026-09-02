/* Second Rail dashboard — plain vanilla JS, no framework, no build step.
 * Polls the read-only API every POLL_INTERVAL_MS and re-renders. The one
 * write path (approve/reject) calls POST /api/approvals/{id}/decide, then
 * immediately re-polls the queue so the UI reflects the change without a
 * manual refresh — see decideApproval() below.
 */
(() => {
  "use strict";

  const POLL_INTERVAL_MS = 1500;

  const state = {
    runId: null,
    sinceSeq: -1,
    resolving: new Set(), // episode_ids with an in-flight decide() call
  };

  const el = {
    pulse: document.getElementById("pulse"),
    runStatus: document.getElementById("run-status"),
    bannerMeta: document.getElementById("banner-meta"),
    stream: document.getElementById("stream"),
    streamHint: document.getElementById("stream-hint"),
    queue: document.getElementById("queue"),
    queueHint: document.getElementById("queue-hint"),
    summaryGrid: document.getElementById("summary-grid"),
    errorSlot: document.getElementById("error-slot"),
  };

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function formatRupees(paise) {
    if (paise === null || paise === undefined) return null;
    return (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function pulseTick() {
    el.pulse.classList.add("tick");
    setTimeout(() => el.pulse.classList.remove("tick"), 200);
  }

  function showError(message) {
    el.errorSlot.innerHTML = `<div class="error-banner">${escapeHtml(message)}</div>`;
  }

  function clearError() {
    el.errorSlot.innerHTML = "";
  }

  async function getJSON(url, options) {
    const res = await fetch(url, options);
    if (res.status === 404) return { ok: false, status: 404, data: null };
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      throw new Error(`${res.status} ${text}`);
    }
    return { ok: true, status: res.status, data: await res.json() };
  }

  // -- episode stream -------------------------------------------------

  function stageTag(stage) {
    return `<span class="tag stage-${escapeHtml(stage || "")}">${escapeHtml(stage || "?")}</span>`;
  }

  function outcomeSpan(outcome) {
    if (!outcome) return "";
    return `<span class="outcome-${escapeHtml(outcome)}">${escapeHtml(outcome)}</span>`;
  }

  function renderRecordLine(rec) {
    const ts = (rec.ts || "").slice(11, 19) || "--:--:--";
    const parts = [
      `<span class="ep-dot">*</span>`,
      `<span class="dim">${escapeHtml(ts)}</span>`,
      escapeHtml(rec.episode_id || "(run)"),
      stageTag(rec.stage),
    ];
    if (rec.outcome) parts.push(outcomeSpan(rec.outcome));
    if (rec.escalation_tier) parts.push(`<span class="dim">${escapeHtml(rec.escalation_tier)}</span>`);
    if (rec.llm && rec.llm.confidence !== undefined && rec.llm.confidence !== null) {
      parts.push(`<span class="dim">conf ${Number(rec.llm.confidence).toFixed(2)}</span>`);
      if (rec.llm.model) parts.push(`<span class="dim">${escapeHtml(rec.llm.model)}</span>`);
    }
    if (rec.execution && rec.execution.plink_id) {
      parts.push(`<span class="dim">${escapeHtml(rec.execution.plink_id)}</span>`);
    }
    if (rec.rationale) {
      parts.push(`<span class="dim">- ${escapeHtml(rec.rationale)}</span>`);
    }
    return `<div class="ep-row"><div class="ep-line">${parts.join(" ")}</div></div>`;
  }

  function appendRecords(records) {
    if (records.length === 0) return;
    if (el.stream.querySelector(".empty-state")) el.stream.innerHTML = "";
    // column-reverse container: append in feed order, CSS flips display order
    // so the newest arrival still lands visually at the top.
    const frag = document.createDocumentFragment();
    for (const rec of records) {
      const wrapper = document.createElement("div");
      wrapper.innerHTML = renderRecordLine(rec);
      frag.appendChild(wrapper.firstElementChild);
    }
    el.stream.appendChild(frag);
  }

  // -- run summary -------------------------------------------------------

  function renderSummary(summary) {
    const tiles = [
      ["actioned", summary.actioned],
      ["suppressed", summary.suppressed],
      ["pending", summary.pending],
      ["execution_failed", summary.execution_failed],
    ];
    let html = tiles.map(([key, value]) => `
      <div class="stat ${key}">
        <div class="label">${escapeHtml(key.replace("_", " "))}</div>
        <div class="value">${value}</div>
      </div>`).join("");

    if (summary.admissibility_rate !== null && summary.admissibility_rate !== undefined) {
      html += `
      <div class="stat">
        <div class="label">admissibility rate</div>
        <div class="value">${(summary.admissibility_rate * 100).toFixed(0)}%</div>
      </div>`;
    }
    const rupees = formatRupees(summary.net_recovered_paise);
    if (rupees !== null) {
      html += `
      <div class="stat">
        <div class="label">net recovered</div>
        <div class="value">Rs ${rupees}</div>
      </div>`;
    }
    el.summaryGrid.innerHTML = html;
  }

  // -- approval queue -----------------------------------------------------

  function renderQueue(items) {
    if (items.length === 0) {
      el.queue.innerHTML = `<div class="empty-state">Queue empty.</div>`;
      el.queueHint.textContent = "";
      return;
    }
    el.queueHint.textContent = `${items.length} pending`;
    el.queue.innerHTML = items.map((item) => `
      <div class="approval-card" data-episode-id="${escapeHtml(item.episode_id)}">
        <div class="info">
          <div class="ep-id">${escapeHtml(item.episode_id)} &middot; Rs ${formatRupees(item.amount_paise)} &middot; ${escapeHtml(item.chosen_action)}</div>
          <div class="detail">${escapeHtml(item.cause)} &mdash; ${escapeHtml(item.gate_reason)}</div>
        </div>
        <div class="actions">
          <button class="approve" data-action="approve">Approve</button>
          <button class="reject" data-action="reject">Reject</button>
        </div>
      </div>
    `).join("");

    el.queue.querySelectorAll(".approval-card").forEach((card) => {
      const episodeId = card.dataset.episodeId;
      card.querySelectorAll("button").forEach((btn) => {
        btn.addEventListener("click", () => decideApproval(episodeId, btn.dataset.action, card));
      });
    });
  }

  async function decideApproval(episodeId, action, card) {
    if (state.resolving.has(episodeId)) return;
    state.resolving.add(episodeId);
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    card.classList.add(action === "approve" ? "resolving-approve" : "resolving-reject");

    try {
      const { ok, data } = await getJSON(`/api/approvals/${encodeURIComponent(episodeId)}/decide`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, reason: action === "reject" ? "dashboard_operator" : null }),
      });
      if (!ok) throw new Error("decide failed");
      clearError();
      // the semaphore-drop: let the border-colour transition play, then
      // collapse the card, then re-poll so the queue reflects reality
      // rather than trusting our own optimistic removal.
      setTimeout(() => {
        card.classList.add("collapsed");
        setTimeout(() => pollQueueOnce(), 260);
      }, 250);
    } catch (err) {
      showError(`approve/reject failed: ${err.message}`);
      card.querySelectorAll("button").forEach((b) => (b.disabled = false));
      card.classList.remove("resolving-approve", "resolving-reject");
    } finally {
      state.resolving.delete(episodeId);
    }
  }

  async function pollQueueOnce() {
    try {
      const { data } = await getJSON("/api/approvals/pending");
      renderQueue(data || []);
    } catch (err) {
      showError(`could not load approval queue: ${err.message}`);
    }
  }

  // -- main poll loop -------------------------------------------------

  async function pollOnce() {
    try {
      const latest = await getJSON("/api/runs/latest");
      if (!latest.ok) {
        el.runStatus.textContent = "no active run";
        el.stream.innerHTML = `<div class="empty-state">No active run.</div>`;
        clearError();
        pulseTick();
        return;
      }

      const run = latest.data;
      if (run.run_id !== state.runId) {
        state.runId = run.run_id;
        state.sinceSeq = -1;
        el.stream.innerHTML = "";
      }
      el.runStatus.textContent =
        `run ${run.run_id} · mode:${run.mode} · cfg:${(run.config_hash || "").slice(0, 8)}`;

      const episodesResp = await getJSON(
        `/api/runs/${encodeURIComponent(state.runId)}/episodes?since=${state.sinceSeq}`
      );
      const records = episodesResp.data || [];
      if (records.length > 0) {
        appendRecords(records);
        state.sinceSeq = records.reduce((max, r) => Math.max(max, r.seq ?? max), state.sinceSeq);
      }

      const summaryResp = await getJSON(`/api/runs/${encodeURIComponent(state.runId)}/summary`);
      if (summaryResp.ok) renderSummary(summaryResp.data);

      clearError();
      pulseTick();
    } catch (err) {
      showError(`poll failed: ${err.message}`);
    }
  }

  async function tick() {
    await pollOnce();
    await pollQueueOnce();
  }

  tick();
  setInterval(tick, POLL_INTERVAL_MS);
})();
