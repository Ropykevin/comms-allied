/* Allied Tours & Travel — shared UI behaviour (vanilla JS, no framework). */
(function () {
  "use strict";

  const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

  /** JSON POST helper that always sends the CSRF token. */
  window.alliedFetch = async function (url, data, options = {}) {
    const response = await fetch(url, {
      method: options.method || "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken(), Accept: "application/json" },
      body: data === undefined ? undefined : JSON.stringify(data),
      credentials: "same-origin",
      signal: options.signal,
    });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    return response.json();
  };

  window.debounce = function (fn, wait = 300) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), wait);
    };
  };

  // Sidebar (mobile)
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");
  const setSidebar = (open) => {
    if (!sidebar) return;
    sidebar.classList.toggle("-translate-x-full", !open);
    if (backdrop) backdrop.hidden = !open;
  };
  document.querySelectorAll("[data-sidebar-open]").forEach((el) => el.addEventListener("click", () => setSidebar(true)));
  document.querySelectorAll("[data-sidebar-close]").forEach((el) => el.addEventListener("click", () => setSidebar(false)));

  // Dropdowns
  document.querySelectorAll("[data-dropdown]").forEach((dd) => {
    const toggle = dd.querySelector("[data-dropdown-toggle]");
    const menu = dd.querySelector("[data-dropdown-menu]");
    toggle?.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
    });
    document.addEventListener("click", (e) => {
      if (!dd.contains(e.target)) menu.hidden = true;
    });
  });

  // Dismissible alerts
  document.querySelectorAll("[data-dismiss]").forEach((btn) =>
    btn.addEventListener("click", () => btn.closest("[data-dismissible]")?.remove())
  );

  // Modals
  window.openModal = (id) => {
    const m = document.getElementById(id);
    if (m) m.hidden = false;
  };
  const closeModal = (m) => {
    if (m) m.hidden = true;
  };
  document.querySelectorAll("[data-modal-open]").forEach((btn) =>
    btn.addEventListener("click", () => window.openModal(btn.dataset.modalOpen))
  );
  document.addEventListener("click", (e) => {
    const closer = e.target.closest("[data-modal-close]");
    if (closer) closeModal(closer.closest("[data-modal]"));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll("[data-modal]").forEach(closeModal);
  });

  // Confirmation dialogs: <form data-confirm="..."> or <button data-confirm="...">
  const confirmModal = document.getElementById("confirm-modal");
  let pendingAction = null;
  window.alliedConfirm = function (message, onAccept, title) {
    if (!confirmModal) {
      if (window.confirm(message)) onAccept();
      return;
    }
    confirmModal.querySelector("[data-confirm-message]").textContent = message;
    confirmModal.querySelector("[data-confirm-title]").textContent = title || "Please confirm";
    pendingAction = onAccept;
    confirmModal.hidden = false;
  };
  confirmModal?.querySelector("[data-confirm-accept]")?.addEventListener("click", () => {
    confirmModal.hidden = true;
    const action = pendingAction;
    pendingAction = null;
    if (action) action();
  });
  document.addEventListener("submit", (e) => {
    const form = e.target;
    const submitter = e.submitter;
    const message = submitter?.dataset.confirm || form.dataset.confirm;
    if (!message || form.dataset.confirmed === "1") return;
    e.preventDefault();
    window.alliedConfirm(message, () => {
      form.dataset.confirmed = "1";
      if (submitter && submitter.name) {
        const hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = submitter.name;
        hidden.value = submitter.value;
        form.appendChild(hidden);
      }
      form.submit();
    });
  });

  // Character counters: <textarea data-counter="#target">
  document.querySelectorAll("[data-counter]").forEach((input) => {
    const target = document.querySelector(input.dataset.counter);
    const update = () => {
      if (target) target.textContent = `${input.value.length} characters`;
    };
    input.addEventListener("input", update);
    update();
  });

  // Select-all checkboxes: <input data-select-all="name">
  document.querySelectorAll("[data-select-all]").forEach((master) => {
    const name = master.dataset.selectAll;
    const boxes = () => document.querySelectorAll(`input[type=checkbox][name="${name}"]`);
    const bar = document.querySelector(`[data-selection-bar="${name}"]`);
    const count = document.querySelector(`[data-selection-count="${name}"]`);
    const refresh = () => {
      const n = [...boxes()].filter((b) => b.checked).length;
      if (bar) bar.hidden = n === 0;
      if (count) count.textContent = n;
    };
    master.addEventListener("change", () => {
      boxes().forEach((b) => (b.checked = master.checked));
      refresh();
    });
    document.addEventListener("change", (e) => {
      if (e.target.name === name) refresh();
    });
    refresh();
  });

  // Auto-submit filter selects: <select data-autosubmit>
  document.querySelectorAll("[data-autosubmit]").forEach((el) =>
    el.addEventListener("change", () => el.form?.submit())
  );
})();
