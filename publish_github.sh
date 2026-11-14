#!/usr/bin/env bash
# Create a private GitHub repo from this folder and push everything to it.
#
#   ./publish_github.sh                        # private repo, roblox-sniper-bot
#   ./publish_github.sh --public               # public repo
#   ./publish_github.sh --public my-bot-name   # public repo, custom name
#
# Needs the GitHub CLI (one-time):  https://cli.github.com
#   macOS:   brew install gh
#   Ubuntu:  sudo apt install gh
#   Windows: winget install GitHub.cli
#
# First run opens a browser once for `gh auth login`. After that it is unattended.

set -euo pipefail
cd "$(dirname "$0")"

VISIBILITY="--private"
REPO_NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --public)  VISIBILITY="--public" ;;
    --private) VISIBILITY="--private" ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*) echo "✗ Unknown option: $1" >&2; exit 2 ;;
    *)  REPO_NAME="$1" ;;
  esac
  shift
done
REPO_NAME="${REPO_NAME:-roblox-sniper-bot}"

if ! command -v gh >/dev/null 2>&1; then
  cat <<'MSG'
✗ GitHub CLI (`gh`) is not installed.

  macOS:   brew install gh
  Ubuntu:  sudo apt install gh
  Windows: winget install GitHub.cli

  Then re-run:  ./publish_github.sh
MSG
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  echo "✗ git is not installed." >&2
  exit 1
fi

# --- local git ---------------------------------------------------------------
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "→ Initialising repository"
  git init -b main >/dev/null
fi

# --- never publish secrets ---------------------------------------------------
if [ -f .env ]; then
  if git check-ignore -q .env; then
    echo "✓ .env is gitignored (secrets stay local)"
  else
    echo "✗ .env is NOT gitignored — refusing to publish." >&2
    echo "  Add '.env' to .gitignore, then re-run." >&2
    exit 1
  fi
fi

# --- one-time browser auth ---------------------------------------------------
if ! gh auth status >/dev/null 2>&1; then
  echo "→ Logging into GitHub (a browser window will open once)"
  gh auth login --web --git-protocol https
fi

git add -A

if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "✗ .env ended up staged — aborting so your secrets are not pushed." >&2
  git reset .env >/dev/null
  exit 1
fi

if ! git diff --cached --quiet; then
  git -c user.email="bot@local" -c user.name="Sniper Bot" commit -m "Roblox sniper bot" >/dev/null
  echo "✓ Committed"
else
  echo "✓ Nothing new to commit"
fi

# --- create + push -----------------------------------------------------------
if git remote get-url origin >/dev/null 2>&1; then
  echo "→ origin already set, pushing"
  git push -u origin main
else
  echo "→ Creating ${VISIBILITY#--} repo '$REPO_NAME' and pushing"
  gh repo create "$REPO_NAME" "$VISIBILITY" --source . --remote origin --push
fi

URL="$(gh repo view --json url -q .url 2>/dev/null || true)"
echo
echo "✓ Done. Repository: ${URL:-$REPO_NAME}"
cat <<'MSG'

Now deploy it on Railway (no extra CLI needed):

  1. railway.com/new  →  "Deploy from GitHub repo"  →  pick this repo
     (railway.json + Dockerfile are already in it, so the build is automatic)
  2. Add these variables:
       DISCORD_TOKEN   = your bot token
       ROBLOX_COOKIE   = .ROBLOSECURITY of the throwaway account
       GUILD_ID        = 1570754134276313299
       DRY_RUN         = true
       DATA_PATH       = /app/data/state.json
  3. Add a volume mounted at /app/data  (otherwise every redeploy forgets
     which items it already bought)
  4. Deploy, then in Discord: /snipe whoami  →  /snipe doctor
     When doctor is green, set DRY_RUN=false and redeploy.

⚠ It must run as a worker/background process — no web port, no health check.
MSG
