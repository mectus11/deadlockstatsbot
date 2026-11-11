# Deploying with Docker

Two ways to run this, depending on what you have:

- **A VPS with SSH access** — follow the steps below; updates are `git pull` + `docker compose up -d --build`.
- **The OVH "Custom Docker image" game panel** (no shell, just the web UI) — see
  [Updating the OVH panel deployment](#updating-the-ovh-panel-deployment) at the bottom instead; the build
  steps below don't apply there. Updates there are one command: `sudo bot-update`.

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
`/save <your steamid>` then `/latestmatch`. `/patchnotes` posts the latest Steam patch note (an admin
runs `/setchannel` once to enable automatic match cards and patch notes).

## Everyday operations

| Task | Command |
|---|---|
| View logs | `docker compose logs -f` |
| Stop it | `docker compose down` |
| Restart it | `docker compose restart` |
| Update after code changes | `git pull` (or re-upload), then `docker compose up -d --build` |
| Add/replace item icons | drop files into `assets/items/` on the VPS — no rebuild needed, just `docker compose restart` |

## What's persisted where

- `./data/bot.db` — linked accounts, the channel to post in, and the patch notes already posted
  (kept so their Hero Changes buttons still work after a restart). **Back this up**; it's the only
  thing that isn't easily rebuilt.
- `./cache/` — downloaded hero/item pictures. Safe to delete; it refills itself.
- `./assets/items/` — your own icons, mounted read-only into the container so you can edit them on the
  VPS directly.
- `.env` — never commit this to git or put it in the zip you move around; it holds your bot token.

## Updating the OVH panel deployment

GitHub Actions rebuilds and publishes `ghcr.io/mectus11/deadlockstatsbot:latest` on every push to `main`
(`.github/workflows/docker-publish.yml`). Updating the VPS is then one command, `bot-update`, which lives
in this repo at `deploy/bot-update.py`.

### Why a script at all

The panel remembers the **id** of the container it created and only ever starts or restarts *that*
container. Recreating the bot — which is what a new image needs — produces a new container with a new
id, so the panel ends up pointing at a dead one and its Start/Restart buttons stop working. `bot-update`
does the recreate and re-points the panel's database in the right order, so the panel keeps working.

### Install (once, on the VPS)

From a clone of this repo:

```bash
sudo install -m 755 deploy/bot-update.py /usr/local/bin/bot-update
```

It runs as root (it needs Docker and the panel's SQLite database), writes to `/var/log/bot-update.log`,
and takes a lock so two runs can't overlap.

### Update

```bash
sudo bot-update --dry-run     # show what would change, touch nothing
sudo bot-update               # update now
sudo bot-update --wait 600    # poll the registry for up to 10 min, then update
sudo bot-update --rollback    # recreate the bot from the previous build
```

The normal loop is: push from your PC → wait for the green check in the Actions tab →
`sudo bot-update --wait 600`. `--wait` polls every 15 s and returns as soon as the new build is
published, so you can start it straight after pushing. If the build is already current it prints
`already up to date` and changes nothing.

### What it does, in order

1. Finds the bot container by its image (not by a hardcoded name), and refuses to guess if several match.
2. Pulls the image and compares its `BUILD_SHA` with the running container's — no-op if unchanged.
3. Backs up the panel database via SQLite's own backup API.
4. Recreates the container from its **own** `docker inspect` output: name, hostname, restart policy,
   network and alias, labels, memory/CPU limits, binds, command and env. Nothing is hardcoded, so panel
   changes can't drift away. Env keys that describe the image itself (`BUILD_SHA`, `PATH`, `LANG`,
   `PYTHON_VERSION`, ...) are always taken from the new image, so the container can't keep reporting the
   old commit. The panel backend is stopped for the swap, so it can't restart the old container mid-way
   and leave two bots connected to Discord.
5. Verifies the new container: running, image id matches, `RestartCount` unchanged, and
   `starting — build <new sha>` present in the logs.
6. Only then swaps the old container id for the new one in the panel database and restarts the backend,
   checking that no stale reference remains.
7. Tags the outgoing image as `<repo>:rollback` and removes the old container, so `--rollback` can
   recreate it. The panel resolves a server's state from its `gamepanel.serverId` label, so keeping a
   second labelled container around (the old `<name>-prev`) made it report the server as stopped while
   the bot was actually running. Leftovers from that older scheme are deleted automatically.

If any step fails, the new container is removed and the previous build is recreated from
`<repo>:rollback` and started; the panel database is left untouched (or the container swap is undone if
the database step failed), and the panel backend is started again — the bot stays up on the old build.

### What it deliberately doesn't touch

- The Valheim server, Traefik, and the panel frontend. Only the bot container and the panel's stored
  container id change.
- The bot's data: `/opt/gamepanel/servers/3/data:/data` (that's where `bot.db` lives).
- The Discord token — that stays in the panel's environment for the server and is carried over verbatim.

Useful flags: `--container <name>` if auto-detection is ambiguous, `--no-panel` to skip the database
step, `--timeout <seconds>` for how long to wait for health. The panel database defaults to
`/opt/gamepanel/data/game-panel.db` and the backend to `gamepanel-backend-1`.
