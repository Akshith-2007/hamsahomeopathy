/* Lab dashboard: upload a report from a modal without leaving the page. */
(function () {
  "use strict";

  const modalEl = document.getElementById("labUploadModal");
  if (!modalEl) return;
  const modal = new bootstrap.Modal(modalEl);
  const form = document.getElementById("labUploadForm");
  const errors = form.querySelector("[data-upload-errors]");
  const submit = form.querySelector("[data-upload-submit]");
  let row = null;

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-upload-url]");
    if (!btn) return;
    row = btn.closest("tr");
    form.action = btn.dataset.uploadUrl;
    form.reset();
    errors.textContent = "";
    form.querySelector("[data-upload-patient]").textContent = btn.dataset.patient;
    form.querySelector("[data-upload-test]").textContent = btn.dataset.test;
    modal.show();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    submit.disabled = true;
    errors.textContent = "";
    try {
      const response = await fetch(form.action, {
        method: "POST", body: new FormData(form), credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" },
      });
      const data = await response.json();
      if (!data.ok) {
        errors.textContent = (data.errors || ["Upload failed."]).join(" ");
        return;
      }
      modal.hide();
      if (row) row.remove();
      Object.entries(data.counts).forEach(([key, value]) => {
        const el = document.querySelector(`[data-count="${key}"]`);
        if (el) el.textContent = value;
      });
      const tbody = document.querySelector("#lab-pending tbody");
      if (tbody && !tbody.querySelector("tr")) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-4">No pending investigations.</td></tr>';
      }
    } catch (err) {
      errors.textContent = "Upload failed. Check the connection and try again.";
    } finally {
      submit.disabled = false;
    }
  });
})();
