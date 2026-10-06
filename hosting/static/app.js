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

const bot = document.querySelector("[data-poll-bot]");
if (bot) {
  // "attach": waiting for the bot to exist; "secretary": waiting for Secretary Mode.
  const want = bot.dataset.pollBot;
  poll("/onboard/bot/status", 3000, (s) => want === "attach" ? s.attached : s.attached && !s.needs_secretary);
}

if (document.querySelector("[data-poll-connected]")) {
  poll("/account/connected", 5000, (s) => s.connected);
}
