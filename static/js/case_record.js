/* Case record page behaviour: uploads, dynamic grids, history modal, repertory chart. */
(function () {
  "use strict";

  const csrfToken = () => {
    const input = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (input) return input.value;
    const match = document.cookie.match(/(?:^|;\s*)hamsa_csrftoken=([^;]+)/);
    return match ? match[1] : "";
  };

  /* ---------------------------------------------------------------- uploads */
  document.querySelectorAll("[data-upload]").forEach((zone) => {
    const input = zone.querySelector("input[type=file]");
    const chosen = zone.querySelector(".chosen");
    const show = () => {
      const names = Array.from(input.files).map((f) => f.name);
      chosen.textContent = names.length ? "Selected: " + names.join(", ") : "";
    };
    input.addEventListener("change", show);
    ["dragenter", "dragover"].forEach((evt) =>
      zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.add("dragover"); }));
    ["dragleave", "drop"].forEach((evt) =>
      zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.remove("dragover"); }));
    zone.addEventListener("drop", (e) => {
      if (!e.dataTransfer.files.length) return;
      const transfer = new DataTransfer();
      const files = input.multiple ? Array.from(e.dataTransfer.files) : [e.dataTransfer.files[0]];
      files.forEach((f) => transfer.items.add(f));
      input.files = transfer.files;
      show();
      markDirty();
    });
  });

  /* ------------------------------------------------- delete buttons (POST) */
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-delete-url]");
    if (!btn) return;
    e.preventDefault();
    if (!confirm("Remove this file?")) return;
    const form = document.createElement("form");
    form.method = "post";
    form.action = btn.dataset.deleteUrl;
    form.innerHTML = `<input type="hidden" name="csrfmiddlewaretoken" value="${csrfToken()}">` +
      `<input type="hidden" name="next" value="${location.pathname}">`;
    document.body.appendChild(form);
    dirty = false;
    form.submit();
  });

  /* ------------------------------------------------------- formset grids */
  const renumber = (table) => {
    let n = 0;
    table.querySelectorAll("tbody tr[data-row]").forEach((row) => {
      row.querySelector(".sn").textContent = row.classList.contains("row-deleted") ? "–" : ++n;
    });
  };
  document.querySelectorAll("table[data-formset]").forEach(renumber);

  document.querySelectorAll("[data-add-row]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const prefix = btn.dataset.addRow;
      const total = document.getElementById(`id_${prefix}-TOTAL_FORMS`);
      const template = document.getElementById(`${prefix}-empty`);
      const table = document.getElementById(`${prefix}-table`);
      const index = parseInt(total.value, 10);
      const html = template.innerHTML.replace(/__prefix__/g, index);
      table.querySelector("tbody").insertAdjacentHTML("beforeend", html);
      total.value = index + 1;
      renumber(table);
      const first = table.querySelector("tbody tr:last-child input:not([type=hidden])");
      if (first) first.focus();
    });
  });

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-del-row]");
    if (!btn) return;
    const row = btn.closest("tr");
    const checkbox = row.querySelector("input[name$='-DELETE']");
    checkbox.checked = !checkbox.checked;
    row.classList.toggle("row-deleted", checkbox.checked);
    btn.innerHTML = checkbox.checked ? '<i class="bi bi-arrow-counterclockwise"></i>' : '<i class="bi bi-trash"></i>';
    renumber(row.closest("table"));
    markDirty();
  });

  /* ------------------------------------------------ expand / collapse all */
  const toggleAll = (show) => document.querySelectorAll("#case-record .collapse").forEach((el) => {
    bootstrap.Collapse.getOrCreateInstance(el, { toggle: false })[show ? "show" : "hide"]();
  });
  document.querySelector("[data-expand-all]")?.addEventListener("click", () => toggleAll(true));
  document.querySelector("[data-collapse-all]")?.addEventListener("click", () => toggleAll(false));

  // Deep links such as #sec-prescription open that section.
  if (location.hash.startsWith("#sec-")) {
    const body = document.querySelector(location.hash + " .collapse");
    if (body) bootstrap.Collapse.getOrCreateInstance(body, { toggle: false }).show();
  }

  /* --------------------------------------------------- previous visit modal */
  const modalEl = document.getElementById("visitModal");
  if (modalEl) {
    const modal = new bootstrap.Modal(modalEl);
    const body = document.getElementById("visitModalBody");
    const openLink = document.getElementById("visitModalOpen");
    const openRow = async (row) => {
      body.innerHTML = '<div class="text-center py-5"><div class="spinner-border text-primary"></div></div>';
      openLink.href = row.dataset.recordUrl;
      modal.show();
      try {
        const response = await fetch(row.dataset.summaryUrl, { headers: { "X-Requested-With": "fetch" } });
        if (!response.ok) throw new Error(response.statusText);
        body.innerHTML = await response.text();
      } catch (err) {
        body.innerHTML = `<div class="alert alert-danger">Could not load visit details (${err.message}).</div>`;
      }
    };
    // Grid rows and "View full visit" buttons (previous prescriptions, timeline).
    document.addEventListener("click", (e) => {
      const trigger = e.target.closest("[data-summary-url]");
      if (trigger) openRow(trigger);
    });
    document.addEventListener("keydown", (e) => {
      const row = e.target.closest(".history-row[data-summary-url]");
      if (row && e.key === "Enter") openRow(row);
    });
  }

  /* ------------------------------------------------ family history insert */
  // Copies the relatives' summary into THIS patient's family-history field only; the doctor edits and saves.
  document.querySelectorAll("[data-insert-family]").forEach((btn) => btn.addEventListener("click", () => {
    const field = document.querySelector(btn.dataset.target);
    if (!field) return;
    const text = btn.dataset.insertFamily;
    field.value = field.value.trim() ? `${field.value.trim()}\n${text}` : text;
    const flag = document.getElementById("family-history-inserted");
    if (flag) flag.value = "1";
    field.focus();
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }));
  document.querySelectorAll("[data-dismiss-target]").forEach((btn) => btn.addEventListener("click", () => {
    document.querySelector(btn.dataset.dismissTarget)?.classList.add("d-none");
  }));

  /* ------------------------------------------- history pre-fill markers */
  // Pre-filled values are only saved when the form is saved; the tag disappears once a field is edited.
  const prefillData = (() => {
    const el = document.getElementById("history-prefill");
    try { return el ? JSON.parse(el.textContent) : null; } catch (e) { return null; }
  })();
  const markPrefilled = (input, label) => {
    clearMark(input);
    input.classList.add("prefilled");
    const tag = document.createElement("div");
    tag.className = "prefill-tag";
    tag.innerHTML = `<i class="bi bi-clock-history"></i> ${label.replace(/[&<>"']/g, "")}`;
    input.insertAdjacentElement("afterend", tag);
    const clear = () => clearMark(input);
    input.addEventListener("input", clear, { once: true });
    input.addEventListener("change", clear, { once: true });
  };
  function clearMark(input) {
    input.classList.remove("prefilled");
    if (input.nextElementSibling?.classList.contains("prefill-tag")) input.nextElementSibling.remove();
  }
  if (prefillData) {
    Object.entries(prefillData.fields).forEach(([name, info]) => {
      const input = document.querySelector(`#case-record [name="${name}"]`);
      if (input && info.prefilled && !input.disabled) markPrefilled(input, info.label);
    });
    // Per-section "Refresh from history" / "Clear".
    prefillData.sections.forEach((section) => {
      const body = document.querySelector(`#body-${section} .sec-body`);
      if (!body || body.querySelector("[data-prefill-tools]")) return;
      const bar = document.createElement("div");
      bar.className = "d-flex justify-content-end gap-1 mb-2";
      bar.dataset.prefillTools = section;
      bar.innerHTML = `<button type="button" class="btn btn-sm btn-outline-secondary" data-refresh><i class="bi bi-arrow-repeat"></i> Refresh from history</button>
        <button type="button" class="btn btn-sm btn-outline-secondary" data-clear><i class="bi bi-eraser"></i> Clear</button>`;
      body.prepend(bar);
      // Only the stable-history fields (never this visit's own complaint, etc.).
      const inputs = () => Object.keys(prefillData.fields).filter((name) => name.startsWith(`${section}-`))
        .map((name) => body.querySelector(`[name="${name}"]`)).filter((input) => input && !input.disabled);
      bar.querySelector("[data-refresh]").addEventListener("click", () => {
        Object.entries(prefillData.fields).forEach(([name, info]) => {
          if (!name.startsWith(`${section}-`)) return;
          const input = body.querySelector(`[name="${name}"]`);
          if (!input || input.disabled) return;
          input.value = info.value;
          markPrefilled(input, info.label);
        });
        markDirty();
      });
      bar.querySelector("[data-clear]").addEventListener("click", () => {
        inputs().forEach((input) => {
          if (input.type === "radio" || input.type === "checkbox") input.checked = false;
          else input.value = "";
          clearMark(input);
        });
        markDirty();
      });
    });
  }

  /* ---------------------------------------------------------- timeline */
  document.querySelectorAll("[data-timeline-filter]").forEach((bar) => {
    const items = bar.parentElement.querySelectorAll(".timeline-item");
    const start = bar.querySelector("[data-filter-start]");
    const end = bar.querySelector("[data-filter-end]");
    const apply = () => items.forEach((item) => {
      const date = item.dataset.date;
      const hidden = (start.value && date < start.value) || (end.value && date > end.value);
      item.classList.toggle("d-none", Boolean(hidden));
    });
    start.addEventListener("change", apply);
    end.addEventListener("change", apply);
    bar.querySelector("[data-filter-clear]").addEventListener("click", () => { start.value = ""; end.value = ""; apply(); });
    bar.querySelectorAll("[data-timeline-expand]").forEach((btn) => btn.addEventListener("click", () => {
      const show = btn.dataset.timelineExpand === "1";
      items.forEach((item) => {
        const body = item.querySelector(".collapse");
        bootstrap.Collapse.getOrCreateInstance(body, { toggle: false })[show ? "show" : "hide"]();
      });
    }));
  });

  /* ----------------------------------------------------- repertory chart */
  const repTable = document.getElementById("rep-table");
  const chart = document.getElementById("rep-chart");
  const MAX_REMEDIES = 8;
  const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const buildChart = () => {
    if (!repTable || !chart) return;
    const rubrics = [];
    repTable.querySelectorAll("tbody tr[data-row]").forEach((row) => {
      if (row.classList.contains("row-deleted")) return;
      const rubric = row.querySelector("input[name$='-rubric']")?.value.trim();
      const weight = parseInt(row.querySelector("select[name$='-grade']")?.value || "1", 10);
      const remedies = {};
      (row.querySelector("input[name$='-remedies']")?.value || "").split(",").forEach((chunk) => {
        const [name, grade] = chunk.split(":").map((s) => s.trim());
        if (name) remedies[name] = Math.min(4, Math.max(1, parseInt(grade, 10) || 1));
      });
      if (rubric) rubrics.push({ rubric, weight, remedies });
    });
    const scores = {};
    rubrics.forEach(({ weight, remedies }) => Object.entries(remedies).forEach(([name, grade]) => {
      scores[name] = scores[name] || { total: 0, count: 0 };
      scores[name].total += grade * weight;
      scores[name].count += 1;
    }));
    const sortKey = document.getElementById("rep-sort").value;
    const names = Object.keys(scores).sort((a, b) =>
      (scores[b][sortKey] - scores[a][sortKey]) || (scores[b].total - scores[a].total)).slice(0, MAX_REMEDIES);
    if (!rubrics.length || !names.length) {
      chart.innerHTML = '<tbody><tr><td class="text-muted small p-3">Enter rubrics with remedies (e.g. <code>Nux-v:3, Sulph:2</code>) and click Generate Chart.</td></tr></tbody>';
      return;
    }
    let html = "<thead><tr><th>#</th><th>Rubric / Symptom</th>" +
      names.map((n) => `<th>${escapeHtml(n)}</th>`).join("") + "</tr></thead><tbody>";
    rubrics.forEach((r, i) => {
      html += `<tr><td>${i + 1}</td><td class="rubric" title="${escapeHtml(r.rubric)}">${escapeHtml(r.rubric)}</td>` +
        names.map((n) => r.remedies[n] ? `<td class="g${r.remedies[n]}">${r.remedies[n]}</td>` : "<td></td>").join("") + "</tr>";
    });
    html += '<tr class="fw-bold"><td></td><td class="rubric">Total (grade × rubric weight)</td>' +
      names.map((n) => `<td>${scores[n].total}</td>`).join("") + "</tr>";
    html += '<tr class="text-muted"><td></td><td class="rubric">Rubrics covered</td>' +
      names.map((n) => `<td>${scores[n].count}</td>`).join("") + "</tr></tbody>";
    chart.innerHTML = html;
  };
  document.getElementById("rep-generate")?.addEventListener("click", buildChart);
  document.getElementById("rep-sort")?.addEventListener("change", buildChart);
  buildChart();

  /* --------------------------------------------------- unsaved changes */
  let dirty = false;
  function markDirty() { dirty = true; }
  const recordForm = document.getElementById("case-record");
  if (recordForm) {
    recordForm.addEventListener("input", markDirty);
    recordForm.addEventListener("change", markDirty);
    recordForm.addEventListener("submit", () => { dirty = false; });
    window.addEventListener("beforeunload", (e) => {
      if (dirty) { e.preventDefault(); e.returnValue = ""; }
    });
  }
})();
