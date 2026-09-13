const $ = (s) => document.querySelector(s),
  esc = (s) =>
    String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
const state = {
  cases: [],
  detail: null,
  tab: "Overview",
  archived: false,
  query: "",
  filter: "all",
  selected: null,
  sourceMode: "pdf",
  comparison: null,
  comparisonPages: {},
  preview: false,
};
const api = async (path = "", data) => {
  const r = await fetch("/api/workspace" + path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Workspace-Request": "1" },
    ...(data ? { method: "POST", body: JSON.stringify(data) } : {}),
  });
  if (r.status === 401) {
    location.assign(
      "/login?view=workspace&case=" +
        encodeURIComponent(state.detail?.id || ""),
    );
    throw Error("Please sign in.");
  }
  const v = await r.json();
  if (!r.ok) throw Error(v.error || "The request could not be completed.");
  return v;
};
const badge = (label, tone = "") =>
  `<span class="pill ${tone}"><span class="dot" aria-hidden="true"></span>${esc(label)}</span>`;
const labels = {
  pending: "Ready to start",
  queued: "Queued",
  in_progress: "Processing",
  running: "Processing",
  complete: "Complete",
  failed: "Needs attention",
  paused: "Paused",
  recovery_required: "Recovery needed",
  open: "Needs review",
  needs_review: "Needs review",
  deferred: "Deferred",
  resolved: "Resolved",
  included: "Medical record",
  excluded: "Excluded",
  duplicate: "Duplicate",
  verified: "Source checked",
};
const statusBadge = (s) =>
  badge(
    labels[s] || s,
    ["failed", "recovery_required"].includes(s)
      ? "red"
      : ["open", "needs_review", "deferred", "paused"].includes(s)
        ? "amber"
        : ["complete", "included", "resolved", "verified"].includes(s)
          ? "teal"
          : "",
  );
const caseStatusBadge = (c) => {
  const status = ["running", "queued"].includes(c.job?.status)
    ? c.job.status
    : c.status;
  return status === "complete"
    ? badge(
        c.review_count ? "Draft · review required" : "Draft generated",
        c.review_count ? "amber" : "teal",
      )
    : statusBadge(status);
};
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").style.display = "block";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("#toast").style.display = "none"), 5500);
}
function fileURL(doc, page = 1) {
  return `/api/workspace/cases/${encodeURIComponent(state.detail.id)}/sources/${encodeURIComponent(doc.id)}#page=${Number(page) || 1}`;
}
function pagePreview(doc, page) {
  const url = fileURL(doc).split("#")[0] + `/pages/${Number(page) || 1}.png`;
  return `<div class="pdf-page"><p class="page-message" role="status">Loading original page…</p><a href="${url}" target="_blank" rel="noopener" aria-label="Enlarge original page ${page} of ${esc(doc.path)}"><img class="source-page-image" src="${url}" alt="Original PDF ${esc(doc.path)}, physical page ${page}" hidden></a></div>`;
}
function metrics(items) {
  return `<div class="metrics">${items.map(([n, l, d]) => `<div class="metric"><span class="label">${l}</span><span class="number">${esc(n)}</span><div class="detail">${d}</div></div>`).join("")}</div>`;
}
function shell(body) {
  return `${state.preview ? '<div class="preview">Interactive preview · fictional records · no patient data or external processing</div>' : ""}<div class="layout"><aside class="sidebar"><div class="brand"><span class="brandmark" aria-hidden="true">≋</span><div>Medical Chronology<small>Case workspace</small></div></div><nav aria-label="Main navigation"><button data-action="cases" class="${!state.archived ? "selected" : ""}"><span class="nav-icon" aria-hidden="true">▦</span>Cases</button><button data-action="archives" class="${state.archived ? "selected" : ""}"><span class="nav-icon" aria-hidden="true">▤</span>Archive</button></nav><div class="side-bottom">Original records preserved<br>Medical records only<br><br><a href="/?legacy=1">Legacy workspace</a><br><a href="/logout">Sign out</a></div></aside><div><header class="topbar"><span>Precision Life Care Planning <span aria-hidden="true">/</span> ${state.detail ? "Case review" : "Your workspace"}</span><span>Team access<span class="avatar" aria-hidden="true">PL</span></span></header><main id="main" class="content">${body}</main></div></div>`;
}
function casesView() {
  const list = state.cases.filter((c) =>
    `${c.name} ${c.id}`.toLowerCase().includes(state.query.toLowerCase()),
  );
  const active = state.cases.filter((c) =>
    ["queued", "running", "in_progress"].includes(c.job?.status || c.status),
  ).length;
  const review = state.cases.filter((c) => c.review_count).length;
  return `<div class="pagehead"><div><p class="eyebrow">${state.archived ? "PRESERVED CASES" : "YOUR CASELOAD"}</p><h1>${state.archived ? "Case archive" : "Your medical chronologies"}</h1><p class="muted">${state.archived ? "Restore a case with its original records, decisions and outputs." : "Track progress, resolve questions, and check every entry against its source."}</p></div><button class="primary" data-action="new">＋ New case</button></div>${metrics(
    [
      [
        state.cases.length,
        state.archived ? "Archived cases" : "All cases",
        "Saved with their source records",
      ],
      [active, "In progress", "Work continues when you close this page"],
      [review, "Need review", "Specific questions for your team"],
      [
        state.cases.filter((c) => c.status === "complete").length,
        "Generated",
        "Review and export status stay separate",
      ],
    ],
  )}<div class="sectionbar"><h2>${state.archived ? "Archived cases" : "Cases"}</h2><input class="search" id="case-search" aria-label="Search cases" placeholder="Search by patient or case name…" value="${esc(state.query)}"></div>${list.length ? `<div class="tablewrap"><table><thead><tr><th>Patient / case</th><th>Progress</th><th>Review</th><th class="case-detail">Records</th><th></th></tr></thead><tbody>${list.map((c) => `<tr><td><button class="casename" ${state.archived ? "disabled" : ""} data-action="open" data-id="${esc(c.id)}">${esc(c.name)}</button><small>${c.legacy ? "Legacy run · original rules preserved" : "Medical records only"}${c.doi ? " · Injury " + esc(c.doi) : ""}</small></td><td>${caseStatusBadge(c)}<div class="progress" aria-hidden="true"><span style="width:${c.status === "complete" ? 100 : Math.min(95, (Object.values(c.phases || {}).filter((p) => p.status === "complete").length / 6) * 100)}%"></span></div></td><td>${c.review_count ? badge(c.review_count + " to review", "amber") : badge(c.status === "complete" ? "Human review required" : "No open questions")}</td><td class="case-detail">${esc(c.documents_count ?? "—")}<small>${c.entries_count || 0} chronology entries</small></td><td><button data-action="${state.archived ? "restore" : "open"}" data-id="${esc(c.id)}">${state.archived ? "Restore" : "Open →"}</button></td></tr>`).join("")}</tbody></table></div>` : `<div class="empty"><h2>${state.query ? "No matching cases" : state.archived ? "No archived cases" : "Your next chronology starts here"}</h2><p>${state.query ? "Try another patient or case name." : "Add a Dropbox records folder to begin."}</p></div>`}`;
}
function caseView() {
  const c = state.detail;
  return `<button class="quiet back" data-action="cases">← All cases</button><div class="pagehead"><div><p class="eyebrow">${c.legacy ? "LEGACY CASE" : "MEDICAL RECORDS CHRONOLOGY"}</p><h1>${esc(c.name)}</h1><p class="muted small">${c.dob ? "DOB " + esc(c.dob) + " · " : ""}${c.doi ? "Injury " + esc(c.doi) + " · " : ""}${c.documents.length} original records</p></div><div class="actions">${caseStatusBadge(c)}<button data-action="archive" ${c.job && ["queued", "running"].includes(c.job.status) ? "disabled" : ""}>Archive</button>${!c.legacy ? `<button class="primary" data-action="${c.job && ["queued", "running"].includes(c.job.status) ? "pause" : "run"}">${c.job && ["queued", "running"].includes(c.job.status) ? "Pause safely" : c.status === "pending" ? "Start processing" : "Run / Resume"}</button>` : ""}</div></div>${c.legacy ? '<div class="callout"><div><strong>Original run preserved</strong><p>This case keeps its historical scope and checkpoints. Use the legacy workspace to continue it; new cases use the medical-only workflow.</p></div><a class="button" href="/?legacy=1&session_id=' + encodeURIComponent(c.id) + '">Open legacy run</a></div>' : ""}<div class="tabs" role="tablist" aria-label="Case sections">${["Overview", "Records", "Chronology", "Review", "Exports"].map((t) => `<button role="tab" aria-selected="${state.tab === t}" class="${state.tab === t ? "active" : ""}" data-action="tab" data-tab="${t}">${t}${t === "Review" && c.review_count ? `<span class="count">${c.review_count}</span>` : ""}</button>`).join("")}</div><section role="tabpanel" aria-label="${state.tab}">${{ Overview: overview, Records: records, Chronology: chronology, Review: review, Exports: exportsView }[state.tab]()}</section>`;
}
function overview() {
  const c = state.detail,
    open = c.issues.filter((i) => ["open", "deferred"].includes(i.status));
  return `${open.length ? `<div class="callout warning"><div aria-hidden="true">◉</div><div><strong>${open.length} source question${open.length === 1 ? "" : "s"} to review</strong><p>Unaffected medical records can continue. Deferred items remain visible until your team resolves them.</p></div><button data-action="tab" data-tab="Review">Review now →</button></div>` : ""}${c.last_error ? `<div class="callout warning"><div><strong>Processing needs attention</strong><p>${esc(c.last_error)}</p></div></div>` : ""}${metrics(
    [
      [c.documents.length, "Original records", "Read-only source files"],
      [
        c.entries.length,
        "Chronology entries",
        "With retained source references",
      ],
      [
        c.documents.filter((d) => d.status === "excluded").length,
        "Excluded records",
        "Reasons remain available",
      ],
      [open.length, "Review questions", "Named decisions saved in history"],
    ],
  )}<div class="two"><div class="card"><h2>Processing progress</h2><p class="muted small">${esc(c.job?.data?.message || "Saved progress is available whenever you return.")}</p>${c.job?.heartbeat ? `<p class="muted small">Worker last checked in ${new Date(c.job.heartbeat * 1000).toLocaleString()}.</p>` : ""}<ol class="steps">${[
    ["download", "Collect original records"],
    ["ocr", "Read and check each page"],
    ["generate", "Build the medical chronology"],
    ["header", "Prepare Word and review files"],
    ["summary", "Prepare summary and quality notes"],
    ["upload", "Deliver to Dropbox"],
  ]
    .map(
      ([k, l], n) =>
        `<li><span class="stepnum">${c.phases?.[k]?.status === "complete" ? "✓" : n + 1}</span><span class="steptext">${l}</span>${statusBadge(c.phases?.[k]?.status || "pending")}</li>`,
    )
    .join(
      "",
    )}</ol></div><div class="card"><h2>What belongs in this chronology</h2><p class="small">Medical encounters, imaging, procedures, treatment response and clinical evaluations. Detail follows the record and the encounter.</p><ul class="note-list"><li>IME and medical expert reports are clearly attributed.</li><li>Depositions and administrative/legal material are excluded.</li><li>Clinical reports enclosed with correspondence are retained.</li><li>${c.policy?.billing_only ? "Billing-only records are explicitly identified." : "Billing-only records remain in the excluded inventory."}</li></ul><button data-action="tab" data-tab="Records">Explore the records →</button></div></div>`;
}
function selectedDoc() {
  const c = state.detail;
  let ref = state.selected;
  if (ref?.document_id)
    return c.documents.find((d) => d.id === ref.document_id);
  return c.documents.find((d) => d.id === ref?.id) || c.documents[0];
}
function sourcePanel() {
  const d = selectedDoc();
  if (!d)
    return '<div class="empty">Choose an entry or record to see its original source.</div>';
  const page = state.selected?.evidence?.[0]?.page || state.selected?.page || 1;
  return `<aside class="card sourcepanel" aria-label="Original source"><div class="sourcehead"><div class="sectionbar"><h3>Original source</h3><div class="actions"><button data-action="source-mode" data-mode="pdf" aria-pressed="${state.sourceMode === "pdf"}">PDF</button><button data-action="source-mode" data-mode="text" aria-pressed="${state.sourceMode === "text"}">OCR text</button></div></div><p>${esc(d.path)} · ${d.page_count ? d.page_count + " pages" : "Page count unknown"}</p><div class="actions" style="margin-top:12px"><label style="margin:0">PDF page <select id="source-page" aria-label="Physical PDF page" style="width:85px;display:inline-block">${Array.from({ length: d.page_count || 1 }, (_, i) => `<option ${i + 1 === page ? "selected" : ""}>${i + 1}</option>`).join("")}</select></label><a href="${fileURL(d, page)}" target="_blank" rel="noopener">Open original ↗</a></div>${state.selected?.quote ? `<details><summary>Supporting passage</summary><p class="small">${esc(state.selected.quote)}</p></details>` : ""}</div>${state.sourceMode === "pdf" ? pagePreview(d, page) : `<pre>${esc((d.pages || []).find((p) => p.page === page)?.text || "No reliable OCR text for this page. Check the original PDF.")}</pre>`}</aside>`;
}
function records() {
  const c = state.detail;
  if (state.comparison) return compareView();
  const list = c.documents.filter(
    (d) => state.filter === "all" || d.status === state.filter,
  );
  return `<div class="sectionbar"><div><h2>Source records</h2><p class="muted small">Inclusion is based on content, with a reason for every exclusion.</p></div></div><div class="filter-row">${[
    ["all", "All records"],
    ["included", "Medical"],
    ["excluded", "Excluded"],
    ["needs_review", "Needs review"],
    ["duplicate", "Duplicates"],
  ]
    .map(
      ([k, l]) =>
        `<button class="${state.filter === k ? "active" : ""}" data-action="filter" data-filter="${k}">${l}</button>`,
    )
    .join(
      "",
    )}</div><div class="workspace"><div>${list.map((d) => `<article class="entry ${selectedDoc()?.id === d.id ? "selected" : ""}"><div class="entryhead">${statusBadge(d.status || "pending")}<span class="small muted">${d.page_count || "—"} pages</span></div><h3 class="document-name">${esc(d.path)}</h3><p class="small muted">${esc(d.reason || "Ready for source review.")}</p>${d.duplicate_of ? '<p class="small">' + (d.duplicate_kind === "exact" ? "An identical file is preserved in the inventory." : "A related source is available for comparison.") + "</p>" : ""}<div class="entryfoot"><span>${d.sha256 ? "Source version " + esc(d.sha256.slice(0, 10)) : "Legacy source"}</span><div class="actions"><button data-action="source" data-id="${esc(d.id)}">View source</button>${d.status === "excluded" ? `<button data-action="reconsider" data-id="${esc(d.id)}">Reconsider</button>` : ""}${d.decision_revision ? `<button data-action="restore-source" data-id="${esc(d.id)}">Reopen review</button>` : ""}${d.duplicate_of ? `<button data-action="compare" data-id="${esc(d.id)}" data-other="${esc(d.duplicate_of)}">Compare</button>` : ""}</div></div></article>`).join("") || '<div class="empty">No records in this category.</div>'}</div>${sourcePanel()}</div>`;
}
function chronology() {
  const c = state.detail;
  return `<div class="sectionbar"><div><h2>Chronology</h2><p class="muted small">Read the narrative and open its supporting original pages.</p></div><button data-action="tab" data-tab="Exports">Word & exports →</button></div>${c.review_count ? '<div class="callout warning"><div><strong>Draft · manual review required</strong><p>Open questions and any withheld material are listed in Review. Check their scope before final use.</p></div></div>' : ""}<div class="workspace"><div>${c.entries.map((e) => `<article class="entry ${state.selected?.id === e.id ? "selected" : ""}"><div class="entryhead"><span class="entrydate">${esc(e.date || "Date needs review")}</span>${badge({ clinical_care: "Medical encounter", medical_evaluation: "Medical evaluation", diagnostic_test: "Diagnostic report", medical_billing: "Billing record" }[e.record_type] || "Medical encounter", "teal")}</div><p>${esc(e.text)}</p>${e.evidence?.length ? `<details class="citations"><summary>Source pages (${e.evidence.length})</summary>${e.evidence.map((r, n) => `<button data-action="citation" data-id="${esc(e.id)}" data-ref="${n}">${esc(c.documents.find((d) => d.id === r.document_id)?.path || "Source")} · PDF page ${r.page}</button>`).join("")}</details>` : ""}<div class="entryfoot"><span>${e.evidence?.length ? e.evidence.length + " source references" : "Legacy provenance not established"}</span><button data-action="entry" data-id="${esc(e.id)}" ${e.document_id ? "" : "disabled"}>Check original →</button></div></article>`).join("") || '<div class="empty"><h2>No chronology entries yet</h2><p>Eligible medical records appear here as processing completes.</p></div>'}</div>${sourcePanel()}</div>`;
}
function review() {
  const c = state.detail;
  return `<div class="sectionbar"><div><h2>Review questions</h2><p class="muted small">Several questions may refer to one document. Check its original and choose what happens next; the document decision covers its related questions.</p></div></div><div class="workspace"><div>${c.issues.map((i) => `<article class="issue"><div class="entryhead">${statusBadge(i.status)}<span class="small muted">${esc(i.kind.replaceAll("_", " "))}</span></div><h3>${esc(i.title || i.kind)}</h3><p>${esc(i.reason)}</p>${i.proposed_entry?.text ? `<details><summary>Withheld proposed entry</summary><p>${esc(i.proposed_entry.text)}</p></details>` : ""}${i.proposed_entries ? `<details><summary>Entries awaiting consolidation</summary>${i.proposed_entries.map((e) => `<p>${esc(e.text)}</p>`).join("")}</details>` : ""}<p class="small">${esc(c.documents.find((d) => d.id === i.document_id)?.path || "Case review")}${i.page ? " · PDF page " + i.page : ""}</p><div class="actions">${!i.document_id ? '<button data-action="export">Retry companion reports</button>' : ""}<button data-action="issue-source" data-id="${esc(i.id)}" ${i.document_id ? "" : "disabled"}>Check original</button>${i.compare_document_id ? `<button data-action="issue-compare" data-id="${esc(i.id)}">Compare both</button>` : ""}<button class="primary" data-action="review" data-id="${esc(i.id)}" ${i.document_id ? "" : "disabled"}>${i.status === "resolved" ? "Review decision" : "Record decision"}</button></div></article>`).join("") || '<div class="empty"><h2>No open review questions</h2><p>Generated content still requires your team’s source review before final use.</p></div>'}<details><summary>Decision history (${c.history.length})</summary>${c.history.map((h) => `<div class="entry"><strong>${esc(h.reviewer)} · ${esc(h.action)}</strong><p>${esc(h.reason)}</p><small>${new Date(h.created * 1000).toLocaleString()}</small></div>`).join("")}</details></div>${sourcePanel()}</div>`;
}
function exportsView() {
  const c = state.detail;
  return `<div class="two"><div class="card"><h2>Download the current version</h2><p class="muted small">Word preserves the dated narrative format. Review information stays available alongside it.</p>${c.artifacts.length ? c.artifacts.map((a) => `<div class="export-row"><div><strong>${esc(a.label || a.name)}</strong><small>${esc(a.version || "Saved output")} ${a.bytes ? "· " + Math.ceil(a.bytes / 1024) + " KB" : ""}</small></div><a class="button" href="/api/workspace/cases/${encodeURIComponent(c.id)}/artifacts/${encodeURIComponent(a.id)}">Download</a></div>`).join("") : '<div class="empty">Exports will appear after generation.</div>'}${!c.legacy ? '<button style="margin-top:18px" data-action="export">Regenerate exports</button>' : ""}</div><div class="card"><h2>Dropbox delivery</h2>${statusBadge(c.phases?.upload?.status || "pending")}<p class="small muted" style="margin-top:15px">${esc(c.destination_folder || "Separate case output folder")}</p><p class="small">Uploaded files are checked against the saved output. An uploaded draft can still have unresolved review questions.</p><h3 style="margin-top:25px">Word format</h3><p class="small muted">Times New Roman, 12pt. Dated paragraphs with patient, DOB and injury heading. Detail follows the encounter.</p></div></div>`;
}

async function loadSourceText() {
  const d = selectedDoc();
  if (!d) return;
  const page = state.selected?.evidence?.[0]?.page || state.selected?.page || 1;
  const value = await api(
    "/cases/" +
      encodeURIComponent(state.detail.id) +
      "/sources/" +
      encodeURIComponent(d.id) +
      "/text?page=" +
      page,
  );
  d.pages ||= [];
  let p = d.pages.find((p) => p.page === page);
  if (!p) {
    p = { page };
    d.pages.push(p);
  }
  p.text =
    value.text +
    (value.correction
      ? "\n\nREVIEWED CORRECTION (original OCR above is preserved)\n" +
        value.correction
      : "");
}
function compareView() {
  const docs = state.comparison
    .map((id) => state.detail.documents.find((d) => d.id === id))
    .filter(Boolean);
  return `<div class="sectionbar"><div><h2>Compare original records</h2><p class="small muted">Check dates, signatures, findings and revisions before deciding whether these are duplicates.</p></div><button data-action="close-compare">Back to records</button></div><div class="workspace">${docs
    .map((d) => {
      const page = state.comparisonPages[d.id] || 1;
      return `<div class="card sourcepanel"><div class="sourcehead"><h3>${esc(d.path)}</h3><p>Source version ${esc(d.sha256?.slice(0, 12) || "unknown")}</p><div class="actions"><label>PDF page <select data-compare-page="${esc(d.id)}" aria-label="Physical PDF page for ${esc(d.path)}">${Array.from({ length: d.page_count || 1 }, (_, i) => `<option ${i + 1 === page ? "selected" : ""}>${i + 1}</option>`).join("")}</select></label><a target="_blank" rel="noopener" href="${fileURL(d, page)}">Open original ↗</a></div></div>${pagePreview(d, page)}</div>`;
    })
    .join("")}</div>`;
}
function render() {
  const focused = document.activeElement?.id,
    pos = document.activeElement?.selectionStart;
  $("#app").innerHTML = shell(state.detail ? caseView() : casesView());
  if (focused === "case-search") {
    $("#case-search").focus();
    $("#case-search").setSelectionRange(pos, pos);
  }
}
async function loadCases() {
  state.detail = null;
  state.selected = null;
  const r = await api("/cases?archived=" + Number(state.archived));
  state.cases = r.cases;
  state.preview = !!r.preview;
  history.replaceState({}, "", location.pathname);
  render();
}
async function openCase(id) {
  const r = await api("/cases/" + encodeURIComponent(id));
  if (state.detail?.id !== r.id) {
    state.selected = r.entries[0] || r.documents[0] || null;
    state.comparison = null;
  }
  state.detail = r;
  state.preview = !!r.preview;
  if (state.sourceMode === "text") await loadSourceText();
  history.replaceState({}, "", "?case=" + encodeURIComponent(id));
  render();
}
async function showDecision(target, action) {
  state.selected = target;
  if (state.sourceMode === "text") await loadSourceText();
  render();
  const f = $("#decision-form");
  f.reset();
  $("#advanced-review-tools").open = false;
  f.elements.target.value = target.id;
  if (target.page) f.elements.page.value = target.page;
  f.elements.fingerprint.value = target.fingerprint || target.sha256 || "";
  if (action) {
    if (["rerun_include", "exclude"].includes(action))
      f.elements.action.value = action;
    else {
      f.elements.advanced_action.value = action;
      $("#advanced-review-tools").open = true;
    }
  }
  updateDecisionAction();
  const document = state.detail.documents.find(
    (d) => d.id === (target.document_id || target.id),
  );
  $("#decision-scope").textContent =
    "Applies to the entire document: " +
    (document?.path || "selected source") +
    ". Original files and decision history are preserved.";
  $("#decision-context").textContent =
    target.reason || target.title || target.path || "Source review";
  f.querySelector(".form-error").textContent = "";
  $("#decision-dialog").showModal();
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-action]");
  if (!b) return;
  const a = b.dataset.action;
  try {
    if (a === "new") {
      $("#new-form").reset();
      $("#new-case").showModal();
      return;
    }
    if (a === "close-dialog") {
      b.closest("dialog").close();
      return;
    }
    if (a === "cases" || a === "archives") {
      state.archived = a === "archives";
      await loadCases();
      return;
    }
    if (a === "open") {
      state.tab = "Overview";
      await openCase(b.dataset.id);
      return;
    }
    if (a === "tab") {
      state.tab = b.dataset.tab;
      state.comparison = null;
      render();
      return;
    }
    if (a === "filter") {
      state.filter = b.dataset.filter;
      render();
      return;
    }
    if (a === "source-mode") {
      state.sourceMode = b.dataset.mode;
      if (state.sourceMode === "text") await loadSourceText();
      render();
      return;
    }
    if (a === "citation") {
      const entry = state.detail.entries.find((x) => x.id === b.dataset.id),
        ref = entry.evidence[Number(b.dataset.ref)];
      state.selected = {
        id: entry.id,
        document_id: ref.document_id,
        page: ref.page,
        quote: ref.quote,
      };
      if (state.sourceMode === "text") await loadSourceText();
      render();
      return;
    }
    if (["source", "entry", "issue-source", "compare"].includes(a)) {
      const c = state.detail;
      state.selected =
        a === "entry"
          ? c.entries.find((x) => x.id === b.dataset.id)
          : a === "issue-source"
            ? c.issues.find((x) => x.id === b.dataset.id)
            : c.documents.find((x) => x.id === b.dataset.id);
      if (a === "compare") {
        state.comparison = [b.dataset.id, b.dataset.other];
        state.tab = "Records";
      }
      if (state.sourceMode === "text") await loadSourceText();
      render();
      return;
    }
    if (a === "close-compare") {
      state.comparison = null;
      render();
      return;
    }
    if (a === "issue-compare") {
      const i = state.detail.issues.find((x) => x.id === b.dataset.id);
      state.comparison = [i.document_id, i.compare_document_id];
      state.tab = "Records";
      render();
      return;
    }
    if (a === "review") {
      await showDecision(
        state.detail.issues.find((x) => x.id === b.dataset.id),
      );
      return;
    }
    if (a === "restore-source") {
      await showDecision(
        state.detail.documents.find((x) => x.id === b.dataset.id),
        "restore",
      );
      return;
    }
    if (a === "reconsider") {
      await showDecision(
        state.detail.documents.find((x) => x.id === b.dataset.id),
        "reconsider",
      );
      return;
    }
    const id = b.dataset.id || state.detail?.id;
    if (["run", "pause", "export", "archive", "restore"].includes(a)) {
      b.disabled = true;
      await api("/cases/" + encodeURIComponent(id) + "/" + a, {});
      toast(
        {
          run: "Processing queued. You can leave this page.",
          pause: "Pause requested at the next safe checkpoint.",
          export: "Export job queued.",
          archive: "Case archived. All files preserved.",
          restore: "Case restored.",
        }[a],
      );
      if (a === "archive" || a === "restore") await loadCases();
      else await openCase(id);
    }
  } catch (err) {
    toast(err.message);
    b.disabled = false;
  }
});
document.addEventListener("input", (e) => {
  if (e.target.id === "case-search") {
    state.query = e.target.value;
    render();
  }
});
document.addEventListener("change", async (e) => {
  if (e.target.dataset.comparePage) {
    state.comparisonPages[e.target.dataset.comparePage] = Number(
      e.target.value,
    );
    render();
    return;
  }
  if (e.target.id === "source-page") {
    const d = selectedDoc();
    state.selected = { document_id: d.id, page: Number(e.target.value) };
    try {
      if (state.sourceMode === "text") await loadSourceText();
      render();
    } catch (err) {
      toast(err.message);
    }
  }
});
$("#new-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target,
    v = Object.fromEntries(new FormData(f));
  try {
    const c = await api("/cases", {
      ...v,
      policy: {
        template: v.template,
        billing_only: f.elements.billing_only.checked,
        medical_expert_reports: f.elements.medical_expert_reports.checked,
      },
    });
    $("#new-case").close();
    state.tab = "Overview";
    await openCase(c.id);
    toast("Case created. Start processing when ready.");
  } catch (err) {
    f.querySelector(".form-error").textContent = err.message;
  }
});
function updateDecisionAction() {
  const f = $("#decision-form");
  f.elements.action.required = !f.elements.advanced_action.value;
  f.elements.reason.required = !!f.elements.advanced_action.value;
}
$("#decision-form").addEventListener("change", (e) => {
  const f = e.currentTarget;
  if (e.target.name === "action") f.elements.advanced_action.value = "";
  if (e.target.name === "advanced_action" && e.target.value)
    f.elements.action.value = "";
  updateDecisionAction();
});
$("#decision-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target,
    v = Object.fromEntries(new FormData(f));
  try {
    await api("/cases/" + encodeURIComponent(state.detail.id) + "/review", {
      ...v,
      action: v.advanced_action || v.action,
      page: v.page ? Number(v.page) : null,
    });
    $("#decision-dialog").close();
    await openCase(state.detail.id);
    toast(
      "Decision saved. Use Run / Resume after your review to update outputs.",
    );
  } catch (err) {
    f.querySelector(".form-error").textContent = err.message;
  }
});
async function refresh() {
  if (document.querySelector("dialog[open]")) return;
  try {
    if (
      state.detail &&
      state.detail.job &&
      ["running", "queued"].includes(state.detail.job.status)
    )
      await openCase(state.detail.id);
  } catch (err) {
    toast(err.message);
  }
}
const initial = new URLSearchParams(location.search).get("case");
(initial ? openCase(initial) : loadCases()).catch(
  (e) =>
    ($("#app").innerHTML = shell(
      `<div class="error" role="alert">${esc(e.message)}</div>`,
    )),
);
setInterval(refresh, 12000);

// Show an explicit fallback instead of a blank pane when a PDF cannot render.
for (const event of ["load", "error"]) {
  document.addEventListener(
    event,
    (e) => {
      if (!e.target.matches?.(".source-page-image")) return;
      const message = e.target
        .closest(".pdf-page")
        .querySelector(".page-message");
      e.target.hidden = event === "error";
      message.hidden = event === "load";
      if (event === "error")
        message.textContent =
          "Page preview unavailable. Use Open original to inspect the PDF.";
    },
    true,
  );
}
