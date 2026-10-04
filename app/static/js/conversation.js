/* Conversation thread: composer tabs, scroll to latest, and polling for new messages/status changes. */
(function () {
  "use strict";
  const thread = document.getElementById("thread");
  if (!thread) return;

  const scroller = document.getElementById("thread-scroll");
  if (scroller) scroller.scrollTop = scroller.scrollHeight;

  document.querySelectorAll("[data-composer-tab]").forEach((tab) =>
    tab.addEventListener("click", () => {
      const name = tab.dataset.composerTab;
      document.querySelectorAll("[data-composer]").forEach((el) => (el.hidden = el.dataset.composer !== name));
      document.querySelectorAll("[data-composer-tab]").forEach((t) => {
        const active = t === tab;
        t.classList.toggle("bg-brand-50", active);
        t.classList.toggle("text-brand-700", active);
        t.classList.toggle("text-slate-500", !active);
      });
    })
  );

  const LABELS = { queued: "Queued", sending: "Sending", sent: "Sent", delivered: "Delivered", read: "Read", failed: "Failed", received: "Received" };
  let latest = parseInt(thread.dataset.latestId || "0", 10);
  const poll = async () => {
    if (document.hidden) return;
    try {
      const data = await alliedFetch(thread.dataset.pollUrl, undefined, { method: "GET" });
      Object.entries(data.statuses).forEach(([id, status]) => {
        const el = document.querySelector(`[data-message-status="${id}"]`);
        if (el) el.textContent = LABELS[status] || status;
      });
      if (data.latest_id > latest) {
        latest = data.latest_id;
        document.getElementById("new-messages").hidden = false;
      }
    } catch (err) {
      console.error(err);
    }
  };
  setInterval(poll, 10000);
})();
