#!/usr/bin/env bash
#
# Initialise the git repo and push it to GitHub.
#
# Run from the project root. Requires the GitHub CLI (`gh`) to be installed and
# authenticated: `gh auth login`.
#
# Your git identity is NOT set by this script on purpose — commits authored with
# the wrong email do not get attributed to your GitHub profile, which defeats
# the point of having the repo.

set -euo pipefail

REPO_NAME=${REPO_NAME:-payments-platform}
VISIBILITY=${VISIBILITY:-public}

# --- sanity checks ---------------------------------------------------------
command -v git >/dev/null || { echo "git not found"; exit 1; }

if ! git config user.email >/dev/null 2>&1; then
  echo "Set your git identity first:"
  echo "  git config --global user.name  \"Your Name\""
  echo "  git config --global user.email \"you@example.com\""
  echo "Use the email attached to your GitHub account."
  exit 1
fi

if [ -f .env ]; then
  echo "note: .env exists locally and is gitignored — good. Never commit it."
fi

# --- init and commit -------------------------------------------------------
git init -b main
git add .
git status --short

cat <<'MSG'

About to create the first commit. Review the file list above — in particular,
confirm that .env is NOT listed.

MSG
read -rp "Continue? [y/N] " ok
[ "$ok" = "y" ] || exit 1

git commit -m "feat: payments platform with double-entry ledger and Kafka outbox

Milestones 1-2:
- docker compose stack: Postgres, Redis, Kafka (KRaft), Kafka UI
- double-entry ledger with balanced-transaction and funds invariants
- payment state machine with explicit legal transitions
- idempotency enforced by unique indexes, not application checks
- transactional outbox + relay process for at-least-once publication
- Kafka consumer with durable dedup on processed_events
- Alembic migrations, unit tests, CI, ADRs"

# --- push ------------------------------------------------------------------
if command -v gh >/dev/null; then
  gh repo create "$REPO_NAME" --"$VISIBILITY" --source=. --push \
    --description "Payments and trading backend on a double-entry ledger with a Kafka transactional outbox"
  echo
  echo "Pushed. Now do this by hand, it matters:"
  echo "  - add topics on the repo: kafka, fastapi, postgresql, event-driven, system-design"
  echo "  - commit per milestone from here on, not one big dump"
else
  echo "gh not installed. Create the repo on github.com, then:"
  echo "  git remote add origin git@github.com:<you>/$REPO_NAME.git"
  echo "  git push -u origin main"
fi
