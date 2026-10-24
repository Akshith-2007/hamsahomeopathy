/* Notification bell: polls /api/notifications/unread/ (paused while the tab is hidden),
 * shows toasts, plays a short sound and raises browser notifications when allowed.
 * Transport lives in `fetchUnread` only, so a WebSocket feed can replace polling later.
 */
(function () {
  "use strict";

  const bell = document.getElementById("notif-bell");
  if (!bell) return;
  const POLL_MS = 12000;
  const countEl = bell.querySelector("[data-notif-count]");
  const listEl = bell.querySelector("[data-notif-list]");
  const toasts = document.getElementById("notif-toasts");
  const soundBtn = bell.querySelector("[data-notif-sound]");
  const desktopBtn = bell.querySelector("[data-notif-desktop]");
  const store = {
    get(key, fallback) { try { return localStorage.getItem(key) ?? fallback; } catch (e) { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* storage blocked */ } },
  };
  const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const csrf = () => (document.cookie.match(/(?:^|;\s*)hamsa_csrftoken=([^;]+)/) || [])[1] || "";
  const openUrl = (id) => bell.dataset.openUrl.replace(/0\/open\/$/, `${id}/open/`);
  let lastSeen = parseInt(store.get("hamsa-notif-last", "0"), 10) || 0;
  let first = true;

  const soundOn = () => store.get("hamsa-notif-sound", "on") === "on";
  const renderSoundBtn = () => { soundBtn.textContent = `Sound: ${soundOn() ? "on" : "off"}`; };
  soundBtn.addEventListener("click", () => { store.set("hamsa-notif-sound", soundOn() ? "off" : "on"); renderSoundBtn(); });
  renderSoundBtn();

  const renderDesktopBtn = () => {
    if (!("Notification" in window)) { desktopBtn.classList.add("d-none"); return; }
    desktopBtn.textContent = Notification.permission === "granted" ? "Desktop alerts on" : "Enable desktop alerts";
    desktopBtn.disabled = Notification.permission !== "default";
  };
  desktopBtn.addEventListener("click", () => Notification.requestPermission().then(renderDesktopBtn));
  renderDesktopBtn();

  const beep = () => {
    if (!soundOn()) return;
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.15, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.4);
    } catch (e) { /* audio blocked until the user interacts */ }
  };

  const toast = (n) => {
    const el = document.createElement("div");
    el.className = "toast border-0 shadow";
    el.setAttribute("role", "status");
    el.innerHTML = `<div class="toast-header ${n.kind === "PRESCRIPTION_UPDATED" ? "text-bg-warning" : "text-bg-success"}">
        <i class="bi bi-capsule me-2"></i><strong class="me-auto">${escapeHtml(n.title)}</strong>
        <button type="button" class="btn-close" data-bs-dismiss="toast" aria-label="Close"></button></div>
      <div class="toast-body">${escapeHtml(n.message)}<div class="mt-2"><a class="btn btn-sm btn-primary" href="${openUrl(n.id)}">Open</a></div></div>`;
    toasts.appendChild(el);
    new bootstrap.Toast(el, { delay: 12000 }).show();
    el.addEventListener("hidden.bs.toast", () => el.remove());
  };

  const desktop = (n) => {
    if (!("Notification" in window) || Notification.permission !== "granted" || !document.hidden) return;
    const alert = new Notification(n.title, { body: n.message, tag: `hamsa-${n.id}` });
    alert.onclick = () => { window.focus(); location.href = openUrl(n.id); };
  };

  const render = (data) => {
    countEl.textContent = data.count > 99 ? "99+" : data.count;
    countEl.classList.toggle("d-none", !data.count);
    listEl.innerHTML = data.results.length ? "" : '<div class="list-group-item small text-muted">No new notifications.</div>';
    data.results.forEach((n) => {
      const item = document.createElement("a");
      item.className = "list-group-item list-group-item-action small";
      item.href = openUrl(n.id);
      item.innerHTML = `<div class="fw-semibold">${escapeHtml(n.title)}</div><div class="text-muted">${escapeHtml(n.message)}</div>
        <div class="text-muted">${new Date(n.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</div>`;
      listEl.appendChild(item);
    });
  };

  async function fetchUnread() {
    const response = await fetch(bell.dataset.unreadUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" });
    if (!response.ok) throw new Error(response.status);
    return response.json();
  }

  const poll = async () => {
    if (document.hidden) return;
    try {
      const data = await fetchUnread();
      render(data);
      const fresh = data.results.filter((n) => n.id > lastSeen);
      if (fresh.length && !first) {
        fresh.slice(0, 3).forEach((n) => { toast(n); desktop(n); });
        beep();
        // Keep the pharmacy queue current without the user reloading.
        if (document.querySelector("[data-pharmacy-queue]")) setTimeout(() => location.reload(), 1500);
      } else if (fresh.length && first) {
        fresh.slice(0, 1).forEach(toast);
      }
      if (data.results.length) {
        lastSeen = Math.max(lastSeen, ...data.results.map((n) => n.id));
        store.set("hamsa-notif-last", String(lastSeen));
      }
      first = false;
    } catch (e) { /* offline or session expired; try again next tick */ }
  };

  bell.querySelector("[data-notif-read-all]").addEventListener("click", async () => {
    await fetch(bell.dataset.readAllUrl, { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": csrf() } });
    poll();
  });

  poll();
  setInterval(poll, POLL_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

  // Pharmacy queue: also refresh every minute so status changes by colleagues show up.
  if (document.querySelector("[data-pharmacy-queue]")) {
    setInterval(() => { if (!document.hidden) location.reload(); }, 60000);
  }
})();
