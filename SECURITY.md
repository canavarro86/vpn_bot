# Security Notes — HideWay VPN Bot

## Current posture (known risks)

### Bot process runs as root

`systemd/hideway-bot.service` has no `User=` directive, so the bot runs as root (UID 0). The motivation is:

- Writing `/usr/local/etc/xray/config.json` (owned by root)
- Running `systemctl reload xray` / `systemctl restart xray`

**Risk:** A vulnerability in the bot (e.g., template injection, path traversal in admin commands, dependency compromise) gives the attacker full root on the VPN server. Combined with the bot's ability to write arbitrary content to the Xray config file, this is high-severity.

### Redundant sudoers entry

The server additionally has:

```
hidewaybot ALL=(ALL) NOPASSWD: ALL
```

This is **redundant** (the process is already root) and **dangerous** — it creates a second escalation path if the process ever runs as `hidewaybot` instead of root.

---

## Recommended minimal-privilege setup

### 1. Dedicated user with scoped sudo

```bash
# Create a dedicated user with no login shell
useradd --system --no-create-home --shell /usr/sbin/nologin hideway-bot

# Give it ownership of the Xray config file
chown hideway-bot:hideway-bot /usr/local/etc/xray/config.json

# Give it ownership of the data directory
chown -R hideway-bot:hideway-bot /opt/hideway-bot/data
```

### 2. Scoped sudoers alias (instead of ALL=(ALL) NOPASSWD:ALL)

Create `/etc/sudoers.d/hideway-bot`:

```sudoers
# Allow the bot to reload/restart xray only — no other sudo rights
Cmnd_Alias HIDEWAY_XRAY = /usr/bin/systemctl reload xray, \
                           /usr/bin/systemctl restart xray

hideway-bot ALL=(root) NOPASSWD: HIDEWAY_XRAY
```

Then in the bot code, the subprocess calls become:

```python
subprocess.run(["sudo", "/usr/bin/systemctl", "reload", "xray"], ...)
```

### 3. Update systemd unit

```ini
[Service]
User=hideway-bot
Group=hideway-bot
```

### 4. Remove the `hidewaybot ALL=(ALL) NOPASSWD: ALL` sudoers line

```bash
visudo -f /etc/sudoers.d/hidewaybot
# Delete or replace the line
```

---

## Other notes

### Rate-limit state is in-memory

`RestartSec=5` in the service means crash loops reset `_hits`/`_violations` deques every 5 seconds, bypassing rate limiting. However, **bans are persisted to SQLite**, so once a user is auto-banned, the ban survives restarts. An attacker aware of the restart cycle gets at most 5 seconds of unmetered access per crash. Acceptable trade-off; document it in ops runbooks.

### Bot token in environment

`TELEGRAM_BOT_TOKEN` is read from `.env` at startup and never written to logs. The `.env` file is in `.gitignore`. Verify it is not world-readable on the server:

```bash
chmod 600 /opt/vpn_bot/.env
```

### DB_ARH legacy data

`DB_ARH/*.json` files contain real Telegram IDs, ban records, and Shadowsocks access URLs from the previous Outline deployment. They are excluded from git via `.gitignore` (as of this commit). The historical git commits that did include them **should be purged** if this repository is made public:

```bash
# Requires git-filter-repo (pip install git-filter-repo)
git filter-repo --path DB_ARH/ --invert-paths
git push --force origin main   # coordinate with all clones first
```

**Do not push this repo public without purging or confirming DB_ARH is no longer in history.**
