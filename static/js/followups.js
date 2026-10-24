/* Follow-up scheduling helpers: confirmations, quick date picks, reschedule modal. */
(function () {
  "use strict";

  // <form data-confirm="..."> asks before submitting.
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (form.dataset.confirm && !confirm(form.dataset.confirm)) e.preventDefault();
  });

  // Schedule form: confirm before replacing an existing scheduled follow-up.
  document.querySelectorAll("form[data-existing-followup]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      const existing = form.dataset.existingFollowup;
      if (!existing) return;
      if (!confirm(`This patient already has a follow-up on ${existing}. Replace it?`)) {
        e.preventDefault();
        return;
      }
      form.querySelector("input[name$='replace']").value = "True";
    });
  });

  // Quick picks: <button data-pick-date="2026-11-01" data-target="#id">
  document.addEventListener("click", (e) => {
    const pick = e.target.closest("[data-pick-date]");
    if (!pick) return;
    const input = document.querySelector(pick.dataset.target);
    if (input) input.value = pick.dataset.pickDate;
  });

  // Reschedule buttons share one modal.
  const modalEl = document.getElementById("rescheduleModal");
  if (modalEl) {
    const modal = new bootstrap.Modal(modalEl);
    const form = document.getElementById("rescheduleForm");
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-reschedule-url]");
      if (!btn) return;
      form.action = btn.dataset.rescheduleUrl;
      form.elements.scheduled_date.value = btn.dataset.date || "";
      form.elements.scheduled_time.value = btn.dataset.time || "";
      form.elements.next.value = btn.dataset.next || location.pathname;
      modalEl.querySelector("[data-patient-label]").textContent = btn.dataset.patient || "";
      modal.show();
    });
  }
})();
