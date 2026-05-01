#!/usr/bin/env bash
# sync.sh — pull latest changes, refresh deps, check models. Idempotent + safe.
#
# What it does (in order):
#   1. Detect uncommitted local changes; offer to stash them
#   2. Fetch from origin, show a diff preview of what's new
#   3. Confirm before pulling (unless --yes)
#   4. Reinstall Python deps from requirements.txt
#   5. Re-run setup.sh model check (unless --no-models)
#
# Flags:
#   --dry-run, -n   Show what would change, don't actually pull
#   --yes, -y       Non-interactive: skip confirmation prompts
#   --no-models     Skip the model-file delta check (faster)
#   --help, -h      Show this message and exit
#
# Examples:
#   ./sync.sh                  # interactive, recommended for first-time updaters
#   ./sync.sh --dry-run        # see what would change without pulling
#   ./sync.sh -y --no-models   # fast non-interactive update of code only
#
# Safe to run from anywhere — it cd's to the script dir first.

set -euo pipefail

# ── Args ────────────────────────────────────────────────────────────────
DRY_RUN=0
YES=0
NO_MODELS=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=1 ;;
        --yes|-y)     YES=1 ;;
        --no-models)  NO_MODELS=1 ;;
        --help|-h)
            sed -n '2,30p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "unknown flag: $arg (try --help)"; exit 2 ;;
    esac
done

# ── Make path-independent ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if present
[[ -f .env ]] && { set -a; source .env; set +a; }

# ── Colors ──────────────────────────────────────────────────────────────
c_red(){ printf '\033[31m%s\033[0m\n' "$*"; }
c_grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
c_yel(){ printf '\033[33m%s\033[0m\n' "$*"; }
c_blu(){ printf '\033[36m%s\033[0m\n' "$*"; }

REPO_NAME=$(basename "$SCRIPT_DIR")
c_blu "==> $REPO_NAME sync"

# ── 1. Detect local modifications ───────────────────────────────────────
STASH_NEEDED=0
if ! git diff --quiet || ! git diff --cached --quiet; then
    c_yel "[!] You have local uncommitted changes:"
    git status --short
    if [[ $YES -eq 1 ]]; then
        STASH_NEEDED=1
        c_yel "    --yes given: will auto-stash and re-apply after pull."
    elif [[ $DRY_RUN -eq 1 ]]; then
        c_yel "    (dry-run: changes will not be touched)"
    else
        echo ""
        echo "Options:"
        echo "  [s] Stash changes, pull, then re-apply stash (recommended)"
        echo "  [a] Abort sync — commit or stash manually first"
        read -rp "Choose [s/a]: " choice
        case "$choice" in
            s|S) STASH_NEEDED=1 ;;
            *)   c_red "Aborted."; exit 1 ;;
        esac
    fi
fi

# ── 2. Fetch + show what's new ──────────────────────────────────────────
c_blu "[1/3] Fetching latest from origin..."
git fetch origin --quiet

# Resolve the tracked remote branch (main or master)
REMOTE_REF=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/@@' || true)
if [[ -z "$REMOTE_REF" ]]; then
    if git show-ref --verify --quiet refs/remotes/origin/main; then
        REMOTE_REF="origin/main"
    elif git show-ref --verify --quiet refs/remotes/origin/master; then
        REMOTE_REF="origin/master"
    else
        c_red "      ✗ couldn't resolve remote default branch — is 'origin' set?"
        exit 1
    fi
fi

CURRENT=$(git rev-parse HEAD)
INCOMING=$(git rev-parse "$REMOTE_REF")

if [[ "$CURRENT" == "$INCOMING" ]]; then
    c_grn "      ✓ Already up to date with $REMOTE_REF"
    PULL_NEEDED=0
else
    PULL_NEEDED=1
    N_COMMITS=$(git rev-list --count "$CURRENT..$INCOMING")
    N_FILES=$(git diff --name-only "$CURRENT..$INCOMING" | wc -l | tr -d ' ')
    c_yel "      $N_COMMITS new commit(s) on $REMOTE_REF, touching $N_FILES file(s):"
    echo ""
    git log --pretty=format:"      %C(yellow)%h%C(reset) %s %C(dim)(%an, %ar)%C(reset)" \
        "$CURRENT..$INCOMING" | head -20
    echo ""
    echo ""
    c_yel "      Files changed:"
    git diff --name-status "$CURRENT..$INCOMING" | head -30 | sed 's/^/        /'
    if [[ $N_FILES -gt 30 ]]; then
        echo "        … and $((N_FILES - 30)) more"
    fi
    echo ""

    if [[ $DRY_RUN -eq 1 ]]; then
        c_yel "[DRY RUN] Would pull the changes above. Use 'git diff $CURRENT..$INCOMING -- <file>'"
        c_yel "         to see specific file changes. Re-run without --dry-run to apply."
        exit 0
    fi

    if [[ $YES -eq 0 ]]; then
        read -rp "Pull these changes? [y/N]: " confirm
        case "$confirm" in
            y|Y|yes|YES) ;;
            *) c_red "Aborted."; exit 1 ;;
        esac
    fi

    if [[ $STASH_NEEDED -eq 1 ]]; then
        c_blu "      Stashing local changes..."
        git stash push -m "sync.sh auto-stash $(date +%Y%m%d-%H%M%S)" --quiet
    fi

    git pull --ff-only --quiet origin "${REMOTE_REF#origin/}"
    c_grn "      ✓ Pulled $N_COMMITS commit(s)."

    if [[ $STASH_NEEDED -eq 1 ]]; then
        c_blu "      Re-applying your stashed changes..."
        if git stash pop --quiet; then
            c_grn "      ✓ Stash re-applied cleanly."
        else
            c_red "      ✗ Stash pop conflicted. Resolve with 'git status', then:"
            c_red "        git stash drop      (if you've integrated the changes)"
            exit 1
        fi
    fi
fi

# ── 3. Refresh Python deps ──────────────────────────────────────────────
c_blu "[2/3] Refreshing Python dependencies..."
if [[ -f requirements.txt ]]; then
    python -m pip install --quiet -r requirements.txt
    c_grn "      ✓ deps up to date."
else
    c_yel "      no requirements.txt found, skipping."
fi

# ── 4. Model delta-check ────────────────────────────────────────────────
if [[ $NO_MODELS -eq 1 ]]; then
    c_yel "[3/3] --no-models: skipping model check"
elif [[ -x ./setup.sh ]]; then
    c_blu "[3/3] Model delta-check (re-running setup.sh model section)..."
    ./setup.sh 2>&1 | tail -n 40 || true
else
    c_yel "[3/3] no setup.sh, skipping model check"
fi

echo ""
if [[ $PULL_NEEDED -eq 1 ]]; then
    c_grn "==> sync complete — pulled $N_COMMITS commit(s) + refreshed deps"
else
    c_grn "==> sync complete — nothing to pull, deps verified"
fi
