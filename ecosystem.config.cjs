// pm2 config for tg-secretary on Ubuntu.
// Start:    pm2 start ecosystem.config.cjs
// Logs:     pm2 logs tg-secretary
// Restart:  pm2 restart tg-secretary
// Stop:     pm2 stop tg-secretary
// Reload conf after editing this file: pm2 reload ecosystem.config.cjs
//
// Autostart on boot:
//   pm2 start ecosystem.config.cjs
//   pm2 save
//   pm2 startup systemd -u $USER --hp $HOME    # copy/run the sudo line it prints

module.exports = {
  apps: [
    {
      name: "tg-secretary",
      script: "./run.sh",
      interpreter: "bash",
      cwd: __dirname,

      // Restart policy
      autorestart: true,
      max_restarts: 20,
      min_uptime: "60s",
      restart_delay: 5000,
      max_memory_restart: "500M",
      kill_timeout: 8000,

      // Logs
      out_file: "./logs/out.log",
      error_file: "./logs/err.log",
      merge_logs: true,
      time: true,

      // Python output buffering off so logs stream live
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
