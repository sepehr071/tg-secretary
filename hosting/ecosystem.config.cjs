// pm2 config for the hosting control plane.
// Start:  pm2 start hosting/ecosystem.config.cjs && pm2 save
// Logs:   pm2 logs tg-hosting

const path = require("path");
const root = path.resolve(__dirname, "..");

module.exports = {
  apps: [
    {
      name: "tg-hosting",
      script: path.join(root, ".venv/bin/python"),
      args: "-m hosting",
      interpreter: "none",
      cwd: root,
      autorestart: true,
      max_memory_restart: "300M",
      out_file: "./logs/hosting-out.log",
      error_file: "./logs/hosting-err.log",
      merge_logs: true,
      time: true,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
