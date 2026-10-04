/* Campaign wizard: step navigation, live audience estimate, channel exclusions, composer and preview. */
(function () {
  "use strict";
  const form = document.getElementById("campaign-form");
  if (!form) return;

  const LABELS = { sms: "SMS", whatsapp: "WhatsApp", email: "Email" };
  const CONTACT_WORD = { sms: "phone number", whatsapp: "WhatsApp number", email: "email address" };
  const fmt = (n) => Number(n || 0).toLocaleString();
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  let estimate = JSON.parse($("#initial-estimate").textContent);
  const steps = $$("[data-step]", form);
  let current = Math.min(Math.max(parseInt(form.dataset.initialStep || "1", 10), 1), steps.length);

  // ---------- helpers ----------
  const channel = () => $("input[name=channel]:checked", form)?.value || "";
  const content = $("#content");
  const subject = $("#subject");
  const isHtml = $("#is_html");

  const audiencePayload = () => ({
    category_ids: $$("input[name=category_ids]:checked", form).map((i) => i.value),
    tag_ids: $$("input[name=tag_ids]:checked", form).map((i) => i.value),
    statuses: $$("input[name=statuses]:checked", form).map((i) => i.value),
    location: $("#location").value,
  });

  // ---------- step navigation ----------
  function showStep(n) {
    current = n;
    steps.forEach((s) => (s.hidden = parseInt(s.dataset.step, 10) !== n));
    $$("[data-goto-step]").forEach((btn) => {
      const i = parseInt(btn.dataset.gotoStep, 10);
      btn.dataset.state = i < n ? "done" : i === n ? "current" : "todo";
    });
    $("[data-wizard-prev]").hidden = n === 1;
    $("[data-wizard-next]").hidden = n === steps.length;
    const finalStep = n === steps.length;
    const mode = $("input[name=send_mode]:checked")?.value || "now";
    $$("[data-final-action]").forEach((b) => (b.hidden = !finalStep || b.dataset.finalAction !== mode));
    if (finalStep) fillReview();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function validateStep(n) {
    const section = steps[n - 1];
    for (const input of $$("[data-required]", section)) {
      if (!input.value.trim()) {
        flagError(input, input.dataset.required);
        return false;
      }
    }
    if (n === 3 && !channel()) {
      flagError($("#channel-summary"), "Choose a channel to continue.");
      return false;
    }
    if (n === 3 && estimate.channels[channel()].available === 0) {
      flagError($("#channel-summary"), `No clients in this audience can be reached by ${LABELS[channel()]}.`);
      return false;
    }
    if (n === 4 && channel() === "email" && !subject.value.trim()) {
      flagError(subject, "Email campaigns need a subject line.");
      return false;
    }
    return true;
  }

  function flagError(el, message) {
    let note = el.parentElement.querySelector(".js-error");
    if (!note) {
      note = document.createElement("p");
      note.className = "form-error js-error";
      el.insertAdjacentElement("afterend", note);
    }
    note.textContent = message;
    el.focus?.();
  }

  $("[data-wizard-next]").addEventListener("click", () => {
    if (validateStep(current)) showStep(current + 1);
  });
  $("[data-wizard-prev]").addEventListener("click", () => showStep(current - 1));
  $$("[data-goto-step]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const target = parseInt(btn.dataset.gotoStep, 10);
      if (target < current) return showStep(target);
      for (let i = current; i < target; i++) if (!validateStep(i)) return showStep(i);
      showStep(target);
    })
  );
  form.addEventListener("input", (e) => e.target.parentElement?.querySelector(".js-error")?.remove());

  // ---------- audience estimate ----------
  let estimateController;
  const refreshEstimate = debounce(async () => {
    estimateController?.abort();
    estimateController = new AbortController();
    try {
      estimate = await alliedFetch(form.dataset.estimateUrl, audiencePayload(), { signal: estimateController.signal });
      renderEstimate();
    } catch (err) {
      if (err.name !== "AbortError") console.error(err);
    }
  }, 250);

  function renderEstimate() {
    $("#est-total").textContent = fmt(estimate.total);
    for (const ch of Object.keys(LABELS)) {
      const n = fmt(estimate.channels[ch].available);
      $$(`[data-est-channel="${ch}"], [data-channel-available="${ch}"]`).forEach((el) => (el.textContent = n));
    }
    renderChannelSummary();
  }

  function renderChannelSummary() {
    const box = $("#channel-summary");
    const ch = channel();
    if (!ch) {
      box.textContent = "Select a channel to see how many clients it can reach.";
      return;
    }
    const c = estimate.channels[ch];
    const label = LABELS[ch];
    box.replaceChildren();
    const lead = document.createElement("p");
    lead.className = "font-semibold text-slate-900";
    lead.textContent = `${fmt(c.available)} client${c.available === 1 ? "" : "s"} can receive this ${label} campaign.`;
    box.appendChild(lead);
    if (c.excluded > 0) {
      const details = [];
      if (c.missing_contact) details.push(`${fmt(c.missing_contact)} do not have a valid ${CONTACT_WORD[ch]}`);
      if (c.opted_out) details.push(`${fmt(c.opted_out)} have opted out of ${label}`);
      const p = document.createElement("p");
      p.className = "mt-1 text-amber-700";
      p.textContent = `${fmt(c.excluded)} client${c.excluded === 1 ? "" : "s"} will be excluded because ${details.join(" and ")}.`;
      box.appendChild(p);
    }
    box.className = `rounded-xl border p-4 text-sm ${c.available ? "border-emerald-200 bg-emerald-50 text-emerald-800" : "border-rose-200 bg-rose-50 text-rose-800"}`;
  }

  $$("[data-audience]", form).forEach((el) => el.addEventListener(el.type === "text" ? "input" : "change", refreshEstimate));

  // ---------- channel-dependent UI ----------
  function applyChannel() {
    const ch = channel();
    $$("[data-email-only]").forEach((el) => (el.hidden = ch !== "email"));
    $$("[data-sms-only]").forEach((el) => (el.hidden = ch !== "sms"));
    $$("[data-whatsapp-only]").forEach((el) => (el.hidden = ch !== "whatsapp"));
    $$("#template_id option[data-channel]").forEach((opt) => {
      const show = !ch || opt.dataset.channel === ch;
      opt.hidden = !show;
      opt.disabled = !show;
    });
    const sel = $("#template_id");
    if (sel.selectedOptions[0]?.disabled) sel.value = "0";
    $("#preview-chat").hidden = ch === "email";
    $("#preview-email").hidden = ch !== "email";
    renderChannelSummary();
    updateCounter();
    refreshPreview();
  }
  $$("[data-channel-input]").forEach((el) => el.addEventListener("change", applyChannel));

  // ---------- templates ----------
  $("#template_id").addEventListener("change", async (e) => {
    const id = e.target.value;
    if (id === "0") return;
    const load = async () => {
      const url = form.dataset.templateUrl.replace(/0\.json$/, `${id}.json`);
      const t = await alliedFetch(url, undefined, { method: "GET" });
      content.value = t.content || "";
      if (t.subject) subject.value = t.subject;
      if (isHtml) isHtml.checked = !!t.is_html;
      updateCounter();
      refreshPreview();
    };
    if (content.value.trim()) alliedConfirm("Replace your current message with this template?", load, "Use template");
    else load();
  });

  // ---------- composer ----------
  $$("[data-insert-var]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const token = `{{${btn.dataset.insertVar}}}`;
      const start = content.selectionStart ?? content.value.length;
      const end = content.selectionEnd ?? start;
      content.value = content.value.slice(0, start) + token + content.value.slice(end);
      content.focus();
      content.selectionStart = content.selectionEnd = start + token.length;
      updateCounter();
      refreshPreview();
    })
  );

  function smsSegments(text) {
    if (!text) return 0;
    const gsm = /^[\x00-\x7F]*$/.test(text);
    const [single, multi] = gsm ? [160, 153] : [70, 67];
    return text.length <= single ? 1 : Math.ceil(text.length / multi);
  }

  function updateCounter() {
    const n = content.value.length;
    $("#content-counter").textContent = `${fmt(n)} character${n === 1 ? "" : "s"}`;
    const seg = smsSegments(content.value);
    $("#sms-segments").textContent = seg > 1 ? `${seg} SMS parts per recipient (before personalisation)` : seg ? "1 SMS per recipient" : "";
  }

  // ---------- preview ----------
  let previewController;
  const refreshPreview = debounce(async () => {
    previewController?.abort();
    previewController = new AbortController();
    try {
      const data = await alliedFetch(
        form.dataset.previewUrl,
        { channel: channel(), subject: subject.value, content: content.value, is_html: isHtml?.checked, audience: audiencePayload() },
        { signal: previewController.signal }
      );
      $("#preview-client").textContent = data.content ? `as ${data.client_name}${data.is_sample ? " (sample)" : ""}` : "";
      const bubble = $("#preview-bubble");
      bubble.textContent = data.content || "Your message preview appears here.";
      bubble.classList.toggle("text-slate-400", !data.content);
      $("#preview-subject").textContent = data.subject || "(no subject)";
      if (data.email_html) $("#preview-frame").srcdoc = data.email_html;
      const unknown = $("#unknown-vars");
      unknown.hidden = !data.unknown_variables.length;
      unknown.textContent = data.unknown_variables.length
        ? `Unknown variable(s): ${data.unknown_variables.map((v) => `{{${v}}}`).join(", ")}`
        : "";
    } catch (err) {
      if (err.name !== "AbortError") console.error(err);
    }
  }, 350);

  $$("[data-preview-input]", form).forEach((el) =>
    el.addEventListener(el.type === "checkbox" ? "change" : "input", () => {
      updateCounter();
      refreshPreview();
    })
  );

  // ---------- review ----------
  function fillReview() {
    $("#review-name").textContent = $("#name").value || "—";
    const ch = channel();
    $("#review-channel").textContent = ch ? LABELS[ch] : "Not selected";
    const parts = $$("[data-audience]:checked", form).map((i) => i.dataset.label);
    const loc = $("#location").value.trim();
    if (loc) parts.push(`Location: ${loc}`);
    $("#review-audience").textContent = parts.length ? parts.join(" · ") : "All clients (except archived)";
    if (ch) {
      const c = estimate.channels[ch];
      $("#review-recipients").textContent = `${fmt(c.available)} recipients`;
      $("#review-excluded").textContent = c.excluded ? `${fmt(c.excluded)} excluded (no ${CONTACT_WORD[ch]} or opted out)` : "";
    }
  }

  function applySendMode() {
    const mode = $("input[name=send_mode]:checked")?.value || "now";
    $("#schedule-fields").hidden = mode !== "schedule";
    if (current === steps.length) $$("[data-final-action]").forEach((b) => (b.hidden = b.dataset.finalAction !== mode));
  }
  $$("input[name=send_mode]").forEach((el) => el.addEventListener("change", applySendMode));

  // ---------- init ----------
  applyChannel();
  applySendMode();
  renderEstimate();
  showStep(current);
})();
