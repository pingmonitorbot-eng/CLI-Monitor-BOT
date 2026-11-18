# SMS Monitor System

- **Controller** (`controller_bot.py`) → Render (always on)
- **Worker** (`monitor_worker.py`) → GitHub Actions (3.5h loops)
- **MongoDB** → shared config (panels, CLIs, group, flags)

## Repo layout (public)

```
shared_db.py
panels.py
github_actions.py
controller_bot.py
monitor_worker.py
requirements-controller.txt
requirements-worker.txt
Dockerfile
.github/workflows/monitor.yml
```

## 1) MongoDB Atlas

Create free cluster → Database User → Network Access `0.0.0.0/0` → copy URI.

## 2) GitHub (public repo)

1. Create public repo, push this folder.
2. Settings → Secrets and variables → Actions:
   - `BOT_TOKEN`
   - `MONGO_URI`
   - `MONGO_DB` = `sms_monitor`
   - optional: `CHECK_INTERVAL`, `LOOKBACK_MINUTES`
3. Create Personal Access Token (classic): scopes `repo` + `workflow`
4. Note `GITHUB_REPO` = `youruser/yourrepo`

## 3) Render (Controller)

- Runtime: Docker (or Python)
- Env:
  - `BOT_TOKEN`
  - `MONGO_URI`
  - `MONGO_DB=sms_monitor`
  - `GITHUB_TOKEN` = PAT from step 2.3
  - `GITHUB_REPO` = `user/repo`
  - `WORKFLOW_FILE=monitor.yml`
  - `GITHUB_REF=main`
  - `WORKER_MAX_HOURS=3.5`
  - `PORT=10000`
- Start: `python controller_bot.py`
- Keep-alive: UptimeRobot ping your Render URL every 5 min

## 4) Telegram

1. `/start` on controller bot
2. Set Group
3. Add panels / CLIs (same DB as before if same MONGO_URI)
4. Start Monitor → triggers GitHub worker

## Stop old bot

Disable old Render service that ran the all-in-one bot.
