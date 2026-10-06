// Polling only: every form works without this file.
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
