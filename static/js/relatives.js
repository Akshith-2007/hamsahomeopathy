/* Relative picker: search existing patients (identity only) and link them with a relationship.
 * <div data-relatives data-mode="multi|single" data-lookup-url=... [data-phone-input="#id"] [data-exclude=pk]>
 */
(function () {
  "use strict";

  const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const debounce = (fn, ms) => { let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); }; };

  document.querySelectorAll("[data-relatives]").forEach((box) => {
    const mode = box.dataset.mode || "multi";
    const lookupUrl = box.dataset.lookupUrl;
    const exclude = box.dataset.exclude || "";
    const search = box.querySelector("[data-relative-search]");
    const results = box.querySelector("[data-relative-results]");
    const selected = box.querySelector("[data-selected-relatives]");
    const options = box.querySelector("template[data-relationship-options]")?.innerHTML || "";
    const panel = box.querySelector("[data-family-panel]");
    const phoneMatches = box.querySelector("[data-phone-matches]");
    const chosen = new Set();

    const lookup = async (query) => {
      if (!query || query.trim().length < 3) return [];
      const url = `${lookupUrl}?q=${encodeURIComponent(query.trim())}&exclude=${exclude}`;
      const response = await fetch(url, { headers: { "X-Requested-With": "fetch" } });
      return response.ok ? (await response.json()).results : [];
    };

    const describe = (p) => `${escapeHtml(p.full_name)} <span class="text-muted small">${escapeHtml(p.opd_number)} · ${p.age} / ${escapeHtml(p.gender)}${p.phone ? " · " + escapeHtml(p.phone) : ""}</span>`;

    const renderResults = (patients) => {
      results.innerHTML = patients.length ? "" : '<div class="list-group-item small text-muted">No matching patients.</div>';
      patients.forEach((p) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "list-group-item list-group-item-action d-flex justify-content-between align-items-center";
        item.innerHTML = `<span>${describe(p)}</span><span class="badge text-bg-primary">${chosen.has(String(p.id)) ? "Added" : "Select"}</span>`;
        item.disabled = chosen.has(String(p.id));
        item.addEventListener("click", () => choose(p));
        results.appendChild(item);
      });
    };

    const choose = (p) => {
      if (mode === "single") {
        box.querySelector("input[name=relative_id]").value = p.id;
        selected.innerHTML = `<div class="alert alert-primary py-2 small mb-2">Selected: ${describe(p)}</div>`;
        results.innerHTML = "";
        return;
      }
      if (chosen.has(String(p.id))) return;
      chosen.add(String(p.id));
      const row = document.createElement("div");
      row.className = "relative-row d-flex flex-wrap align-items-center gap-2 border rounded p-2 mb-2";
      row.innerHTML = `
        <input type="hidden" name="relative_id" value="${p.id}">
        <div class="flex-fill">${describe(p)}</div>
        <label class="small text-muted mb-0">This relative is the patient's</label>
        <select name="relative_relationship" class="form-select form-select-sm w-auto" required>${options}</select>
        <input name="relative_label" class="form-control form-control-sm w-auto d-none" placeholder="e.g. Cousin">
        <button type="button" class="btn btn-sm btn-outline-danger" title="Remove"><i class="bi bi-x-lg"></i></button>`;
      const select = row.querySelector("select");
      const label = row.querySelector("input[name=relative_label]");
      select.addEventListener("change", () => label.classList.toggle("d-none", select.value !== "OTHER"));
      row.querySelector("button").addEventListener("click", () => { chosen.delete(String(p.id)); row.remove(); });
      selected.appendChild(row);
      results.innerHTML = "";
      if (search) search.value = "";
    };

    search?.addEventListener("input", debounce(async () => renderResults(await lookup(search.value)), 300));

    // Registration: Yes/No toggle and automatic suggestions for the mobile just entered.
    box.querySelectorAll("input[name=has_family]").forEach((radio) => radio.addEventListener("change", () => {
      const yes = box.querySelector("input[name=has_family][value=yes]").checked;
      panel?.classList.toggle("d-none", !yes);
    }));
    const phoneInput = box.dataset.phoneInput ? document.querySelector(box.dataset.phoneInput) : null;
    if (phoneInput && phoneMatches) {
      const suggest = debounce(async () => {
        // Only look up complete-looking numbers, not every keystroke prefix.
        const matches = phoneInput.value.replace(/\D/g, "").length >= 7 ? await lookup(phoneInput.value) : [];
        if (!matches.length) { phoneMatches.classList.add("d-none"); return; }
        phoneMatches.classList.remove("d-none");
        phoneMatches.innerHTML = `<i class="bi bi-people"></i> ${matches.length} patient(s) already registered with this mobile. Family members? `;
        matches.forEach((p) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "btn btn-sm btn-outline-primary ms-1 mb-1";
          btn.innerHTML = `<i class="bi bi-link-45deg"></i> Link ${escapeHtml(p.full_name)}`;
          btn.addEventListener("click", () => {
            box.querySelector("input[name=has_family][value=yes]").checked = true;
            panel?.classList.remove("d-none");
            choose(p);
          });
          phoneMatches.appendChild(btn);
        });
      }, 400);
      phoneInput.addEventListener("input", suggest);
      if (phoneInput.value) suggest();
    }
  });

  // Inline edit toggles on the patient page.
  document.querySelectorAll("select[data-other-toggle]").forEach((select) => {
    const label = document.querySelector(select.dataset.otherToggle);
    const sync = () => label && label.classList.toggle("d-none", select.value !== "OTHER");
    select.addEventListener("change", sync);
    sync();
  });
})();
