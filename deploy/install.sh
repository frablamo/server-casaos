#!/usr/bin/env bash
# media-butler installer for a Linux server running systemd.
#
#   sudo ./deploy/install.sh
#
# Interactive on the first run. If /etc/media-butler/config.toml already exists
# it does not ask again: it only reinstalls the code and restarts the service.
# Pass --reconfigure to answer the questions again and rewrite the config.
set -euo pipefail

INSTALL_DIR="/opt/media-butler"
CONFIG_DIR="/etc/media-butler"
STATE_DIR="/var/lib/media-butler"
CONFIG_FILE="$CONFIG_DIR/config.toml"
SERVICE_FILE="/etc/systemd/system/media-butler.service"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mwarning: %s\033[0m\n' "$*" >&2; }
die() { printf '\033[1;31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# A CR left by a Windows clipboard, a tab or an exotic space is invisible in
# the terminal but makes the systemd unit unparseable.  Strip them before they
# reach the config or the unit file.
clean_input() {
    local value="$1"
    value="${value//$'\r'/}"
    value="${value//$'\t'/}"
    value="$(printf '%s' "$value" \
        | tr -d '\001-\010\013\014\016-\037\177' \
        | sed -e 's/\xc2\xa0/ /g' -e 's/\xe2\x80\x8b//g' -e 's/\xe2\x80\x8c//g' \
              -e 's/\xe2\x80\x8d//g' -e 's/\xef\xbb\xbf//g')"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

ask() {
    local prompt="$1" variable="$2" value
    read -r -p "$prompt" value
    printf -v "$variable" '%s' "$(clean_input "$value")"
}

ask_secret() {
    local prompt="$1" variable="$2" value
    read -r -s -p "$prompt" value
    echo
    printf -v "$variable" '%s' "$(clean_input "$value")"
}

RECONFIGURE=0
for argument in "$@"; do
    case "$argument" in
        --reconfigure) RECONFIGURE=1 ;;
        -h|--help)
            cat <<'USAGE'
Usage: sudo ./deploy/install.sh [--reconfigure]

  --reconfigure   ask for the paths and secrets again and rewrite
                  /etc/media-butler/config.toml (the default only asks on
                  the first install)
USAGE
            exit 0
            ;;
        *) die "unknown option: $argument" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run this installer with sudo"

say "Checking dependencies"
PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null || die "python3 is missing"
"$PYTHON_BIN" - <<'PY' || die "Python 3.11 or newer is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

# 7z: p7zip-full on Debian/Ubuntu, 7zip on Arch/Fedora.
SEVEN_ZIP=""
for candidate in 7z 7zz 7za; do
    if command -v "$candidate" >/dev/null; then
        SEVEN_ZIP="$candidate"
        break
    fi
done
if [[ -z "$SEVEN_ZIP" ]]; then
    warn "7z not found; installing it"
    if command -v apt-get >/dev/null; then
        apt-get update
        apt-get install -y p7zip-full || apt-get install -y 7zip
    elif command -v pacman >/dev/null; then
        pacman -S --noconfirm p7zip
    elif command -v dnf >/dev/null; then
        dnf install -y p7zip
    else
        die "install 7z (p7zip-full) manually and run again"
    fi
    for candidate in 7z 7zz 7za; do
        command -v "$candidate" >/dev/null && SEVEN_ZIP="$candidate" && break
    done
fi
[[ -n "$SEVEN_ZIP" ]] || die "7z is still not available"

say "Installing media-butler into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR"
chmod 700 "$STATE_DIR" "$CONFIG_DIR"

# Clean copy of the package (the venv survives updates).
rm -rf "$INSTALL_DIR/media_butler"
cp -a "$ROOT_DIR/media_butler" "$INSTALL_DIR/"
cp -a "$ROOT_DIR/pyproject.toml" "$INSTALL_DIR/"
if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
    if ! "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv"; then
        warn "could not create the venv; python3-venv is missing"
        if command -v apt-get >/dev/null; then
            apt-get update
            apt-get install -y python3-venv
            "$PYTHON_BIN" -m venv "$INSTALL_DIR/venv" \
                || die "the venv still cannot be created"
        else
            die "install your distribution's venv package"
        fi
    fi
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade "$INSTALL_DIR"

if [[ ! -f "$CONFIG_FILE" || "$RECONFIGURE" -eq 1 ]]; then
    say "Initial configuration"
    echo "The paths must exist; I will create them if needed."
    ask "Download inbox           : " INBOX
    ask "Kids destination         : " KIDS
    ask "Movies destination       : " MOVIES
    ask "Series destination       : " SERIES
    ask "Emby URL (empty = none)  : " EMBY_URL
    if [[ -n "$EMBY_URL" ]]; then
        ask_secret "Emby API key             : " EMBY_KEY
    fi
    ask_secret "Bot token (BotFather)    : " BOT_TOKEN
    ask "Your Telegram user_id (empty = discover with /start): " OWNER_ID

    [[ -n "$INBOX" && -n "$KIDS" && -n "$MOVIES" && -n "$SERIES" ]] \
        || die "all four paths are required"
    [[ "$INBOX" == /* ]] || die "the inbox must be an absolute path: $INBOX"
    [[ "$KIDS" == /* ]] || die "the kids destination must be an absolute path: $KIDS"
    [[ "$MOVIES" == /* ]] || die "the movies destination must be an absolute path: $MOVIES"
    [[ "$SERIES" == /* ]] || die "the series destination must be an absolute path: $SERIES"
    for dir in "$INBOX" "$KIDS" "$MOVIES" "$SERIES"; do
        mkdir -p -m 755 "$dir"
    done

    if [[ -n "${OWNER_ID:-}" && ! "$OWNER_ID" =~ ^[0-9]+$ ]]; then
        die "the user_id must be numeric (the bot tells you when you send /start)"
    fi

    umask 077
    if [[ -z "$BOT_TOKEN" && -f "$CONFIG_DIR/token" ]]; then
        say "Keeping the existing token at $CONFIG_DIR/token"
    else
        [[ -n "$BOT_TOKEN" ]] || die "the token cannot be empty"
        [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] \
            || die "that does not look like a BotFather token (expected 123456789:AA...)"
        install -m 600 /dev/null "$CONFIG_DIR/token"
        printf '%s' "$BOT_TOKEN" > "$CONFIG_DIR/token"
    fi

    EMBY_ENABLED="false"
    if [[ -n "$EMBY_URL" ]]; then
        if [[ -z "${EMBY_KEY:-}" && -f "$CONFIG_DIR/emby.key" ]]; then
            say "Keeping the existing Emby API key"
        else
            [[ -n "${EMBY_KEY:-}" ]] || die "an Emby URL requires an API key"
            install -m 600 /dev/null "$CONFIG_DIR/emby.key"
            printf '%s' "$EMBY_KEY" > "$CONFIG_DIR/emby.key"
        fi
        EMBY_ENABLED="true"
    fi

    "$INSTALL_DIR/venv/bin/python" - "$CONFIG_FILE" "$INBOX" "$KIDS" "$MOVIES" \
        "$SERIES" "$EMBY_ENABLED" "$EMBY_URL" "${OWNER_ID:-}" "$SEVEN_ZIP" <<'PY'
import json
import sys

config_file, inbox, kids, movies, series, emby_enabled, emby_url, owner, seven_zip = sys.argv[1:]
owners = f"[{int(owner)}]" if owner.strip() else "[]"
quote = lambda value: json.dumps(value, ensure_ascii=False)  # noqa: E731

content = f"""# Generated by deploy/install.sh
database = "/var/lib/media-butler/state.db"

[telegram]
token_file = "/etc/media-butler/token"
allowed_users = {owners}
chat_id = 0
delete_finished = true

[paths]
inbox = {quote(inbox)}
kids = {quote(kids)}
movies = {quote(movies)}
series = {quote(series)}

[scan]
interval_seconds = 3600
settle_seconds = 120
max_pending = 10
max_entries = 100000
seven_zip = {quote(seven_zip)}

[behavior]
delete_archive = true
dry_run = false

[emby]
enabled = {emby_enabled}
url = {quote(emby_url if emby_enabled == "true" else "")}
api_key_file = "/etc/media-butler/emby.key"

[reboot]
countdown_seconds = 10
cooldown_seconds = 300
notify_on_boot = true

[logging]
level = "INFO"
"""
with open(config_file, "w", encoding="utf-8") as handle:
    handle.write(content)
PY
    chmod 600 "$CONFIG_FILE"
    if [[ -z "${OWNER_ID:-}" ]]; then
        warn "no allowed_users: start the service, send /start to the bot, copy the ID"
        warn "into $CONFIG_FILE and run: systemctl restart media-butler"
    fi
else
    say "Existing configuration at $CONFIG_FILE (leaving it untouched; use --reconfigure to change it)"
fi

say "Generating systemd unit"
if ! READWRITE="$("$INSTALL_DIR/venv/bin/python" - "$CONFIG_FILE" <<'PY'
import sys

from media_butler.config import ConfigError, load_config

try:
    config = load_config(sys.argv[1])
except ConfigError as exc:
    print(f"error: {exc}", file=sys.stderr)
    raise SystemExit(1)
paths = {config.paths.inbox, *config.paths.destinations.values(), config.database.parent}


def unit_quote(path: str) -> str:
    # systemd expands % specifiers and uses \ and " for quoting, so escape
    # all three before wrapping the path in double quotes.
    escaped = (
        path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    )
    return f'-"{escaped}"'


print(" ".join(unit_quote(path) for path in sorted(map(str, paths))))
PY
)"; then
    die "the configuration is invalid (see the message above); fix $CONFIG_FILE or run again with --reconfigure"
fi

"$INSTALL_DIR/venv/bin/python" - "$ROOT_DIR/deploy/media-butler.service" "$SERVICE_FILE" \
    "$INSTALL_DIR" "$READWRITE" <<'PY'
import sys

source, destination, project, readwrite = sys.argv[1:]
with open(source, encoding="utf-8") as handle:
    unit = handle.read()
unit = unit.replace("@PROJECT@", project).replace("@READWRITE@", readwrite)
with open(destination, "w", encoding="utf-8") as handle:
    handle.write(unit)
PY
chmod 644 "$SERVICE_FILE"

say "Validating installation"
if LC_ALL=C grep -n '[[:cntrl:]]' "$SERVICE_FILE"; then
    die "control character found in $SERVICE_FILE (lines above); run again with --reconfigure"
fi
if command -v systemd-analyze >/dev/null; then
    systemd-analyze verify "$SERVICE_FILE" \
        || die "systemd rejected $SERVICE_FILE (see the messages above); run again with --reconfigure"
fi
"$INSTALL_DIR/venv/bin/media-butler" --config "$CONFIG_FILE" --check

say "Starting service"
systemctl daemon-reload
systemctl enable media-butler >/dev/null
systemctl restart media-butler
sleep 2
systemctl --no-pager --lines=15 status media-butler || true
if ! systemctl is-active --quiet media-butler; then
    warn "media-butler is not running; inspect it with: journalctl -u media-butler -n 50"
fi

cat <<EOF

Done.

  Config:   $CONFIG_FILE
  Token:    $CONFIG_DIR/token
  State:    $STATE_DIR/state.db
  Logs:     journalctl -u media-butler -f

Open Telegram and send /start to the bot. If there is no allowed_users yet,
the bot will reply with your user_id: add it to the config and restart with
  sudo systemctl restart media-butler
EOF
