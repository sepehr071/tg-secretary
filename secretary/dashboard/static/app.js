/* Progressive enhancement only: every page works without this file. */
(() => {
  document.documentElement.classList.add("js");
  const $$ =(s, r = document) => [...r.querySelectorAll(s)];
  const toastEl = document.getElementById("toast");
  const ICON_OK = '<svg class="ic" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l3 3 5-6"/></svg>';
  const ICON_ERR = '<svg class="ic" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v6M12 16.5h.01"/></svg>';
  let toastTimer;

  function toast(text, isErr) {
    if (!toastEl || !text) return;
    clearTimeout(toastTimer);
    toastEl.innerHTML = (isErr ? ICON_ERR : ICON_OK) + "<span></span>";
    toastEl.lastChild.textContent = text;
    toastEl.classList.toggle("is-err", !!isErr);
    toastEl.setAttribute("role", isErr ? "alert" : "status");
    toastEl.hidden = false;
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, isErr ? 4500 : 2200);
  }
  window.toast = toast;

  // Server flash (?msg= / ?err=) arrives in data attributes on the toast element.
  if (toastEl && (toastEl.dataset.msg || toastEl.dataset.err)) {
    toast(toastEl.dataset.err || toastEl.dataset.msg, !!toastEl.dataset.err);
    const u = new URL(location.href);
    if (u.searchParams.has("msg") || u.searchParams.has("err")) {
      u.searchParams.delete("msg"); u.searchParams.delete("err");
      history.replaceState(null, "", u.pathname + u.search + u.hash);
    }
  }

  // form[data-autosave]: POST on change, debounced on typing; no-JS keeps the plain submit button.
  $$("form[data-autosave]").forEach((form) => {
    $$("[data-nojs-submit]", form).forEach((b) => { b.hidden = true; });
    let timer, inflight = false, again = false;
    const save = async () => {
      if (inflight) { again = true; return; }
      inflight = true;
      try {
        const r = await fetch(form.action, { method: form.method || "POST", body: new FormData(form), headers: { "X-Requested-With": "fetch" } });
        // fetch follows the 303, so a back(err=...) redirect still lands as 200
        if (!r.ok || new URL(r.url).searchParams.get("err")) throw new Error(r.status);
        toast(form.dataset.saved || "ذخیره شد");
      } catch (e) {
        toast(form.dataset.failed || "ذخیره نشد. دوباره امتحان کن.", true);
        $$("[data-nojs-submit]", form).forEach((b) => { b.hidden = false; });
      }
      inflight = false;
      if (again) { again = false; save(); }
    };
    form.addEventListener("change", () => { clearTimeout(timer); save(); });
    form.addEventListener("input", (e) => {
      if (e.target.tagName !== "TEXTAREA") return;
      clearTimeout(timer); timer = setTimeout(save, 800);
    });
    form.addEventListener("submit", (e) => { e.preventDefault(); clearTimeout(timer); save(); });
  });

  // Bottom sheets on <dialog>.
  document.addEventListener("click", (e) => {
    const open = e.target.closest("[data-sheet-open]");
    if (open) {
      const d = document.getElementById(open.dataset.sheetOpen);
      if (d && d.showModal) { e.preventDefault(); d.showModal(); }
      return;
    }
    if (e.target.closest("[data-sheet-close]")) {
      const d = e.target.closest("dialog"); if (d) d.close();
      return;
    }
    if (e.target.tagName === "DIALOG" && e.target.classList.contains("sheet")) e.target.close(); // backdrop click
    const cp = e.target.closest("[data-copy]");
    if (cp) {
      const text = cp.dataset.copy;
      const done = () => toast(cp.dataset.copied || "کپی شد");
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, () => fallback(text, done));
      else fallback(text, done);
    }
  });
  function fallback(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch (e) { toast(text, true); }
    ta.remove();
  }

  // Live preview: <el data-preview-for="form-id" data-previews='{"1-0-1":"..."}'> follows the form's
  // radios named tone / len / emoji (key = "tone-len-emoji").
  $$("[data-preview-for]").forEach((el) => {
    const form = document.getElementById(el.dataset.previewFor);
    if (!form) return;
    let map = {};
    try { map = JSON.parse(el.dataset.previews || "{}"); } catch (e) { /* keep empty */ }
    const val = (n) => { const c = form.querySelector(`input[name="${n}"]:checked`); return c ? c.value : ""; };
    const update = () => { const t = map[`${val("tone")}-${val("len")}-${val("emoji")}`]; if (t) el.textContent = t; };
    form.addEventListener("change", update);
    update();
  });
})();
