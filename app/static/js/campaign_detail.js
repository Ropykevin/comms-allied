/* Live-refresh campaign statistics while messages are being sent. */
(function () {
  "use strict";
  const box = document.getElementById("campaign-stats");
  if (!box || box.dataset.live !== "1") return;

  const tick = async () => {
    try {
      const data = await alliedFetch(box.dataset.statsUrl, undefined, { method: "GET" });
      Object.entries(data.stats).forEach(([key, value]) => {
        const el = box.querySelector(`[data-stat="${key}"]`);
        if (el) el.textContent = Number(value).toLocaleString();
      });
      if (!["queued", "sending"].includes(data.status)) {
        window.location.reload();
        return;
      }
    } catch (err) {
      console.error(err);
    }
    setTimeout(tick, 3000);
  };
  setTimeout(tick, 2000);
})();
