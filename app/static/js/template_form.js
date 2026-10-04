(function () {
  "use strict";
  const channel = document.getElementById("template-channel");
  if (!channel) return;
  const sync = () => {
    document.querySelectorAll("[data-template-email]").forEach((el) => (el.hidden = channel.value !== "email"));
    document.querySelectorAll("[data-template-whatsapp]").forEach((el) => (el.hidden = channel.value !== "whatsapp"));
  };
  channel.addEventListener("change", sync);
  sync();
})();
