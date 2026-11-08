# Deploying with Docker

Two ways to run this, depending on what you have:

- **A VPS with SSH access** — follow the steps below; updates are `git pull` + `docker compose up -d --build`.
- **The OVH "Custom Docker image" game panel** (no shell, just the web UI) — see
  [Updating the OVH panel deployment](#updating-the-ovh-panel-deployment) at the bottom instead; the build
  steps below don't apply there.

## Deploying to an OVH Ubuntu VPS with Docker

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

## Updating the OVH panel deployment

GitHub Actions already rebuilds and publishes the image to `ghcr.io/<you>/deadlockstatsbot:latest`
automatically on every push to `main` (`.github/workflows/docker-publish.yml`). What a plain web panel
usually does **not** do on its own is notice that a new image was published and re-pull it — a "Restart"
button in most such panels restarts the *existing* container image, it doesn't fetch a new one. So the loop
is:

1. Push your code change to GitHub.
2. Check the **Actions** tab on your repo — wait for the green checkmark (a minute or two).
3. In the OVH panel, look for whatever action re-pulls the image rather than just restarting the running
   one — often called **Reinstall**, **Redeploy**, or similar (as opposed to plain **Restart**/**Stop+Start**).
   Your `/app/data` volume keeps your saved accounts and channel either way, so this is safe to use.
4. **Confirm it actually updated** by checking the logs for the new startup line:
   `starting — build <commit sha>`. Compare that short SHA to the latest commit on GitHub (Actions tab, or
   the repo's commit history) — if they match, the panel picked up the new image; if the log still shows an
   old or `dev` SHA, that button didn't actually re-pull, and you'll need to look for the right one in the
   panel (or ask its support/docs) or delete-and-reinstall the server.

There's no way to make this fully automatic without shell access to the host (that needs a sidecar like
Watchtower watching Docker directly, which a web-panel-only setup doesn't expose) — but steps 1–4 are a
five-minute loop, not a redeploy from scratch.
