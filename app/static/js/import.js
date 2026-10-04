(function () {
  "use strict";
  const input = document.getElementById("file");
  const label = document.getElementById("file-name");
  if (!input || !label) return;
  input.addEventListener("change", () => {
    const f = input.files[0];
    if (f) label.textContent = `${f.name} · ${(f.size / 1024).toFixed(1)} KB`;
  });
})();
