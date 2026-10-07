// Everything here is an enhancement: every page and form works without this file.
function poll(url, every, done) {
  const tick = async () => {
    try {
      const r = await fetch(url, { credentials: "same-origin", cache: "no-store" });
      if (r.ok && done(await r.json())) return location.reload();
    } catch (e) { /* offline for a moment: try again next tick */ }
    setTimeout(tick, every);
  };
  setTimeout(tick, every);
}

if (document.querySelector("[data-poll-connected]")) {
  poll("/account/connected", 5000, (s) => s.connected && s.can_reply);
}

function toast(text) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const el = document.createElement("div");
  el.className = "toast";
  el.setAttribute("role", "status");
  el.textContent = text;
  document.body.append(el);
  setTimeout(() => el.remove(), 2200);
}

// Copy button: [data-copy="text"]
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b) return;
  try {
    await navigator.clipboard.writeText(b.dataset.copy);
    toast("نام ربات کپی شد");
  } catch (err) {
    toast("کپی نشد؛ نام رو خودت انتخاب کن");
  }
});

// Onboarding: one step at a time, chips, live preview. Without JS all steps are visible in one form.
const ob = document.querySelector("form[data-wizard]");
if (ob) {
  document.querySelectorAll("[data-js]").forEach((el) => { el.hidden = false; });
  ob.classList.add("wizard");
  const steps = [...ob.querySelectorAll("fieldset.step")];
  const bars = [...ob.querySelectorAll(".steps-bar span")];
  const $ = (s) => ob.querySelector(s);
  let i = 0;

  const show = (n) => {
    i = Math.max(0, Math.min(steps.length - 1, n));
    steps.forEach((s, k) => s.classList.toggle("current", k === i));
    bars.forEach((b, k) => b.classList.toggle("on", k <= i));
    const last = i === steps.length - 1;
    $("[data-back]").hidden = i === 0;
    $("[data-next]").hidden = last;
    $("[data-submit]").hidden = !last;
    $("[data-skip]").hidden = i === 0;
    const f = steps[i].querySelector("input[type=text], textarea");
    if (f) f.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  };
  const next = () => {
    const req = steps[i].querySelector("[required]");
    if (req && !req.reportValidity()) return;
    show(i + 1);
  };
  $("[data-next]").addEventListener("click", next);
  $("[data-back]").addEventListener("click", () => show(i - 1));
  // Enter in the name field moves on instead of submitting half a form.
  ob.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.matches("input[type=text]") && i < steps.length - 1) {
      e.preventDefault();
      next();
    }
  });

  // About chips toggle their text inside the textarea.
  const about = $("#about");
  const chips = [...ob.querySelectorAll("[data-chip]")];
  const syncChips = () => chips.forEach((c) => {
    const on = about.value.includes(c.dataset.chip);
    c.classList.toggle("on", on);
    c.setAttribute("aria-pressed", on);
    c.querySelector("svg").innerHTML = on ? '<path d="M5 12.5l4.5 4.5L19 7.5"/>' : '<path d="M12 5v14M5 12h14"/>';
  });
  chips.forEach((c) => c.addEventListener("click", () => {
    const t = c.dataset.chip;
    about.value = about.value.includes(t)
      ? about.value.replace(t, "").replace(/\n{2,}/g, "\n").trim()
      : (about.value.trim() ? about.value.trim() + "\n" : "") + t;
    syncChips();
  }));
  about.addEventListener("input", syncChips);
  syncChips();

  // Live preview: data-previews is {"tone-len-emoji": text}.
  const pv = $("[data-preview-for]");
  const previews = JSON.parse(pv.dataset.previews || "{}");
  const val = (n) => (ob.querySelector(`input[name=${n}]:checked`) || {}).value;
  const upd = () => { pv.querySelector("[data-preview-text]").textContent = previews[`${val("tone")}-${val("len")}-${val("emoji")}`] || ""; };
  ob.addEventListener("change", (e) => { if (e.target.matches("input[type=radio]")) upd(); });
  upd();

  show(0);
}
