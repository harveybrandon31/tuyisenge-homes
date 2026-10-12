#!/bin/bash
# Daily refresh for the Tuyisenge home list. Run by launchd (com.brandon.tuyisenge-homes).
# Pulls listings, rebuilds the page, publishes to GitHub Pages, and shows a Mac notification.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/bin"
REPO="$HOME/tuyisenge-homes"
LOG="$REPO/tools/daily.log"
cd "$REPO" || exit 1
exec >>"$LOG" 2>&1
echo "=== $(date) ==="

notify() {
  /usr/bin/osascript -e "display notification \"$2\" with title \"Tuyisenge home list\" subtitle \"$1\"" || true
}

# clear stale git locks left by a crashed run
find .git -name "*.lock" -mmin +10 -delete 2>/dev/null

git pull -q --rebase || echo "git pull failed, continuing with local copy"

python3 tools/update.py
rc=$?
if [ $rc -ne 0 ]; then
  notify "Refresh failed" "Redfin did not answer today. The list was not changed."
  exit $rc
fi

git add -A
if git diff --cached --quiet; then
  notify "No changes" "Nothing new today. The list is the same."
  exit 0
fi
git -c user.name="Brandon Harvey" -c user.email="brandon@phaseloans.com" commit -q -m "Daily refresh $(date +%Y-%m-%d)"
if ! git push -q; then
  notify "Publish failed" "The list was rebuilt but could not be pushed to GitHub. See tools/daily.log."
  exit 1
fi

summary=$(grep -E '^(ADDED|GONE|PRICE)' changes.md | sed -E 's/  +/ /g' | head -4 | tr '\n' ';' | sed 's/;$//' | tr -d '"')
counts=$(sed -n 3p changes.md | cut -c1-60)
if [ -z "$summary" ]; then notify "No new homes or changes" "$counts"; echo published; exit 0; fi
notify "List updated" "$summary"
echo "published"
