# Saint Rewards Bot

A Telegram group bot for giveaways, raffles, points, leaderboards, and redeemable community rewards.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/saintbypass-byte/saint-rewards-bot)

## Features

- Admin-created giveaways with one-tap entry buttons
- Fair random winner drawing from unique entries
- Member points balances and leaderboards
- Admin point awards by replying to a member
- Per-group reward shop with point redemption
- Render-compatible `/health` endpoint
- SQLite storage with no external database dependency

## Commands

### Members

- `/start` — Open the bot panel
- `/points` — View your points
- `/leaderboard` — Show the top members
- `/rewards` — Show the reward shop
- `/redeem reward name` — Redeem a reward

### Admins

- `/giveaway prize name` — Create a giveaway
- `/draw giveaway_id` — Draw a winner
- Reply to a member with `/award points` — Add or remove points
- `/reward add name | cost` — Add or update a reward
- `/reward remove name` — Remove a reward

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Put your real BOT_TOKEN in .env
python -m saint_rewards.bot
```

## Render free Web Service

Click the Deploy to Render button or create a Web Service from this repository. Choose the Free plan, Docker runtime, and add `BOT_TOKEN` as an environment variable. The service exposes `/health` on Render's `PORT` while Telegram polling runs in the same container.

The free plan has an ephemeral filesystem, so SQLite data can reset after restarts or redeploys. Use a paid persistent disk or external PostgreSQL if points and reward history must survive permanently.

Add the bot to the group and grant administrator rights if you want it to create and manage community giveaways. Keep the bot token private and rotate it through `@BotFather` if it is exposed.
