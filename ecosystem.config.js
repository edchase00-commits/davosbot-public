module.exports = {
  apps: [
    {
      name: "davosbot",
      script: "main.py",
      interpreter: "/Users/<you>/projects/davosbot/venv/bin/python3",
      watch: false,
      restart_delay: 3000,
      max_restarts: 10,
      error_file: "./logs/err.log",
      out_file: "./logs/out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
    {
      name: "davosbot-autodeploy",
      script: "scripts/auto_deploy.py",
      interpreter: "/Users/<you>/projects/davosbot/venv/bin/python3",
      watch: false,
      autorestart: true,
      restart_delay: 30000,
      max_restarts: 10,
      error_file: "./logs/autodeploy-err.log",
      out_file: "./logs/autodeploy-out.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
