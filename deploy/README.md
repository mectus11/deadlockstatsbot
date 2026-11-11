# deploy/

`bot-update.py` — the updater for the bot running under the OVH game panel. Install it once:

```bash
sudo install -m 755 deploy/bot-update.py /usr/local/bin/bot-update
```

Then, on the VPS:

```bash
sudo bot-update --dry-run     # show the plan, change nothing
sudo bot-update               # pull, recreate, re-point the panel
sudo bot-update --wait 600    # wait for a freshly pushed build, then update
sudo bot-update --rollback    # recreate the bot from the previous build
```

Why it exists: the panel stores the **id** of the container it created and only starts/restarts that id,
so recreating the container by hand leaves the panel pointing at a dead one. The script recreates the
container from its own `docker inspect` output, verifies the new build, and only then rewrites the panel's
database — rolling back if the new build doesn't come up.

Full explanation, including what it does and doesn't touch, is in
[../DEPLOY.md](../DEPLOY.md#updating-the-ovh-panel-deployment). Tests are in `../test_deploy.py`.
