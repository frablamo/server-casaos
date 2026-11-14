# media-butler

Telegram bot for the home server. It watches a download folder, extracts what
it finds, and **asks you over Telegram** whether each batch goes to **Kids**,
**Movies** or **Series**. It can also reboot the server from your phone, with
confirmation and a countdown.

```
INBOX ──► archive? ──yes──► 7z (with zip-slip validation)
   │                           │
   └──────────────┬────────────┘
                  ▼
       "🎬 Dune.2021 — 8.4 GB · 1 video"
        [👶 Kids] [🎥 Movies] [📺 Series]
                  [❌ Ignore]
                  ▼
        move to <destination>/Dune.2021
        refresh Emby · delete the .rar
```

## What it does

- Scans the inbox every 5 minutes (or immediately with `/scan`).
- Waits until a file or folder stops changing before touching it (in-progress
  downloads, `.part`, `.crdownload`, `.aria2`, `.!qB`…).
- Extracts `zip`, `rar`, `7z`, `tar`, `tar.gz`, `tar.bz2`, `tar.xz`, `tgz`…
  with `7z`.
- Rejects archives with absolute paths, `..` or symlinks; encrypted ones are
  marked as blocked instead of guessing.
- Asks once per batch; if the bot restarts, the buttons keep working (state in
  SQLite).
- Never overwrites: if the name exists it appends ` (1)`, ` (2)`…
- Moves with `rename` on the same filesystem; across filesystems it copies to a
  hidden temporary sibling, renames it into place, then deletes the source. A
  power cut leaves no silent garbage: startup reconciles it.
- Deletes the archive once all its content moved successfully
  (`delete_archive = false` to keep it).
- Refreshes the **Emby** library after every move.
- `/reboot` restarts the whole server with confirmation, a cancellable
  countdown and a notification when it is back online.
- `--dry-run` mode to test the buttons without moving or rebooting anything.

## Requirements

- Linux with **systemd** (designed for Debian/Ubuntu, also works on Arch/Fedora).
- **Python 3.11+**.
- **7z** (`p7zip-full` on Debian/Ubuntu, `p7zip` on Arch, `p7zip` on Fedora).
- A Telegram bot (token from [@BotFather](https://t.me/BotFather)).
- The media directories must be writable by **root** (the service runs as root;
  that is what allows moving files on root-owned volumes and running
  `systemctl reboot`).

## Installation

```bash
git clone https://github.com/YOUR-USER/server-casaos.git
cd server-casaos
sudo ./deploy/install.sh
```

The installer:

1. Checks/installs `7z`, Python 3.11+ and `python3-venv`.
2. Copies the project to `/opt/media-butler` and creates its venv.
3. Asks for the paths, token, Emby URL/API key and your Telegram `user_id`.
4. Writes `/etc/media-butler/config.toml` (600) and the secrets separately.
5. Generates and starts `media-butler.service`.
6. Runs `media-butler --check` to verify paths, write access and `7z`.

If `/etc/media-butler/config.toml` already exists, it **does not ask again**: it
only updates the code and restarts the service.

### First run on Telegram

1. Open the bot and send `/start`.
2. If there is no `allowed_users` yet, the bot replies with your `user_id`.
3. Add it to `/etc/media-butler/config.toml`:

   ```toml
   [telegram]
   allowed_users = [123456789]
   ```

4. `sudo systemctl restart media-butler`.

Only the IDs in `allowed_users` can use buttons and commands. Anyone else gets
a notice with their own ID and nothing else.

## Configuration

File: `/etc/media-butler/config.toml` (full example in
[`deploy/config.example.toml`](deploy/config.example.toml)).

| Key | Default | Description |
|---|---|---|
| `database` | `/var/lib/media-butler/state.db` | SQLite (WAL) with queue and history |
| `telegram.token_file` | — | 600 file with the BotFather token |
| `telegram.allowed_users` | `[]` | Numeric IDs allowed to use the bot |
| `telegram.chat_id` | first allowed user | chat where prompts are sent |
| `paths.inbox` | — | watched folder |
| `paths.kids` / `movies` / `series` | — | destinations; must exist |
| `scan.interval_seconds` | `300` | scan frequency (min. 30) |
| `scan.settle_seconds` | `120` | unchanged time before touching anything |
| `scan.max_pending` | `10` | unanswered prompts before scanning pauses |
| `scan.max_entries` | `100000` | limit when sizing a directory |
| `scan.seven_zip` | `7z` | 7-Zip binary (`7zz` on some distros) |
| `behavior.delete_archive` | `true` | delete the archive after moving everything |
| `behavior.dry_run` | `false` | simulate moves and reboots |
| `emby.enabled` | on if `url` is set | refresh the library |
| `emby.url` | — | e.g. `http://127.0.0.1:8096` |
| `emby.api_key_file` | — | 600 file with the `X-Emby-Token` |
| `reboot.countdown_seconds` | `10` | time to cancel |
| `reboot.cooldown_seconds` | `300` | minimum time between reboots |
| `reboot.notify_on_boot` | `true` | notify when back after `/reboot` |

The extension lists (`video_extensions`, `archive_extensions`, `skip_suffixes`)
can be overridden in `[scan]`; the defaults already cover the usual cases.

Paths and token are validated on startup. If a directory or the token is
missing, the service will not start and the error appears in `journalctl`.

## Usage

| Command | Action |
|---|---|
| `/scan` | scan the inbox now |
| `/status` | queue, free space per destination, uptime, mode |
| `/reboot` | reboot the server (confirmation + countdown) |
| `/help` | help |

Each pending batch shows:

- **👶 Kids**, **🎥 Movies**, **📺 Series**: move and refresh Emby.
- **❌ Ignore**: leaves it in the inbox and does not ask again about that same
  version of the file (if it changes, it asks again).

Blocked archives (password, unsafe paths, no videos or not enough space) show
**🔄 Retry** in addition to **Ignore**.

If a move fails, the message shows the error and the buttons stay active so you
can retry.

### States

`pending` → `moving` → `done`

- `pending`: waiting for your decision.
- `moving`: move in progress (if the process dies here, startup reconciles it
  against the disk).
- `blocked`: the archive needs a password, contains unsafe paths, has no videos
  or does not fit on disk. Offers **Retry** and **Ignore**.
- `ignored`: stays in the inbox; that content is not asked about again.
- `done` and `ignored` are purged from the database after 30 days.

## Security

The service runs as root because that is the only way to write the media
volumes and reboot the machine without `sudoers` rules. To keep that
reasonable:

- Strict allowlist by `user_id`; everyone else is ignored.
- No `shell=True`: every process is launched with argument lists.
- Paths built with `pathlib`, sanitised names and checked destinations.
- Files are validated **before** extraction (zip-slip, symlinks, encryption).
- Unsafe or encrypted archives are blocked, not forced.
- Token and API key in `600 root:root` files, outside the repository.
- Hardened systemd unit (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`,
  `ReadWritePaths` limited to inbox/destinations/state, `ProtectKernel*`,
  `RestrictSUIDSGID`, `RestrictNamespaces`…).
- Reboot requires confirmation, has a cancellable countdown and a 5-minute
  cooldown.

Even so, this is a root bot with Telegram access: if the token leaks, whoever
has it can move files and reboot the server. Treat it as a sensitive credential
and rotate the token in BotFather if needed.

## Operations

```bash
# service status
systemctl status media-butler
journalctl -u media-butler -f

# validate configuration, paths and 7z (as root)
sudo /opt/media-butler/venv/bin/media-butler --check

# simulate moves and reboots (stop the service first: Telegram only allows one
# polling instance at a time)
sudo systemctl stop media-butler
sudo /opt/media-butler/venv/bin/media-butler --dry-run

# restart / reload after editing the config
sudo systemctl restart media-butler
```

### Update

```bash
cd server-casaos
git pull
sudo ./deploy/install.sh   # keeps /etc/media-butler/config.toml
```

### Uninstall

```bash
sudo systemctl disable --now media-butler
sudo rm /etc/systemd/system/media-butler.service
sudo systemctl daemon-reload
sudo rm -rf /opt/media-butler
# keep /etc/media-butler and /var/lib/media-butler if you may come back
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `7z: ✗ not found` | install `p7zip-full` (Debian/Ubuntu) or set the `7zz` path in `scan.seven_zip` |
| `Permission denied` while moving | the service must run as root; check `ProtectSystem`/`ReadWritePaths` and run `--check` with sudo |
| The bot does not respond | check the token, `journalctl` and that no other instance uses the same token (Telegram allows only one polling) |
| `Conflict: terminated by other getUpdates` | two processes are using the same token; stop one (`systemctl stop media-butler` before a `--dry-run`) |
| `Not authorized` | add your `user_id` to `allowed_users` and restart |
| A `.rar` stays blocked | it may need a password or contain symlinks; check the message and the log |
| Emby does not refresh | try `curl -X POST -H "X-Emby-Token: ..." http://127.0.0.1:8096/Library/Refresh` |
| `/reboot` fails | check `journalctl -u media-butler`; the service needs systemd as PID 1 |
| I want to start over | `systemctl stop media-butler && rm /var/lib/media-butler/state.db && systemctl start media-butler` |

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

The tests cover configuration, name collisions, archive validation, recovery
from an interrupted move, callbacks and the scan/extract logic with a simulated
`7z`. They need no network or Telegram.

Layout:

```
media_butler/
  config.py      TOML, validation and secrets
  db.py          SQLite: items, state, reconciliation, purge
  scanner.py     signature/stability, folders, videos and archives
  archive.py     7z: listing, zip-slip validation, extraction
  organizer.py   crash-safe moves, collisions, temporary files
  emby.py        POST /Library/Refresh
  callbacks.py   button encode/parse (validated)
  bot.py         handlers, queue, reboot with countdown
  __main__.py    CLI: --check, --dry-run, --log-level
deploy/
  install.sh     interactive installer (root)
  media-butler.service
  config.example.toml
tests/
```

## License

Private use. Add the license you prefer before publishing the repository.
