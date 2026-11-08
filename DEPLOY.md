# Deploying to an OVH Ubuntu VPS with Docker

## 1. Get Docker onto the VPS

SSH in, then:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Log out and back in (so the group change takes effect) before continuing. The Docker Compose plugin
(`docker compose ...`, no hyphen) comes bundled with that install.

## 2. Get the bot's files onto the VPS

Easiest is `scp` from your own machine (run this on your PC, not the VPS):

```bash
scp deadlockstatsbot.zip youruser@your-vps-ip:~
```

Then on the VPS:

```bash
sudo apt-get install -y unzip   # if it isn't already there
unzip deadlockstatsbot.zip
cd deadlockstatsbot
```

If you'd rather keep it in git (recommended once it's running, so updates are `git pull`), push this
folder to a private GitHub repo first and `git clone` it on the VPS instead.

## 3. Configure it

```bash
cp .env.docker.example .env
nano .env        # paste in DISCORD_TOKEN, and STATLOCKER_API_KEY once you have one
```

If you have your own item icons (the `assets/items` folder), make sure that folder is here too —
`scp -r assets/items youruser@your-vps-ip:~/deadlockstatsbot/assets/` if you forgot it in the zip.

## 4. Build and run

```bash
docker compose up -d --build
```

`-d` runs it in the background. `restart: unless-stopped` in `docker-compose.yml` means it comes back
on its own after a VPS reboot or crash, without you doing anything.

## 5. Check it's working

```bash
docker compose logs -f
```

Look for the Discord login confirmation (`^C` to stop watching, it keeps running). In Discord, run
`/save <your steamid>` then `/latestmatch`.

## Everyday operations

| Task | Command |
|---|---|
| View logs | `docker compose logs -f` |
| Stop it | `docker compose down` |
| Restart it | `docker compose restart` |
| Update after code changes | `git pull` (or re-upload), then `docker compose up -d --build` |
| Add/replace item icons | drop files into `assets/items/` on the VPS — no rebuild needed, just `docker compose restart` |

## What's persisted where

- `./data/bot.db` — linked accounts and the channel to post in. **Back this up**; it's the only thing
  that isn't easily rebuilt.
- `./cache/` — downloaded hero/item pictures. Safe to delete; it refills itself.
- `./assets/items/` — your own icons, mounted read-only into the container so you can edit them on the
  VPS directly.
- `.env` — never commit this to git or put it in the zip you move around; it holds your bot token.
