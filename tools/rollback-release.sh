#!/usr/bin/env bash
#
# Roll production back to a known-good release.
#
#   tools/rollback-release.sh                 # dry run against pre-3.0.0
#   tools/rollback-release.sh --yes           # commit it
#   tools/rollback-release.sh v2.5.0 --yes    # some other ref
#
# WHY A FORWARD COMMIT, not a revert and not a force-push.
#
# Both Static Web Apps deploy whatever is at the tip of main, and the repo root
# IS the deployed site (app_location: "/"). So the fastest way back is a new
# commit on main whose TREE is the old release. That:
#
#   - triggers the normal deploy on both apps, no manual Azure step;
#   - leaves history intact, so the rolled-back work is still reachable and can
#     be re-applied by restoring the newer tree the same way;
#   - avoids `git revert -m 1` on the 3.0.0 merge, which would make any later
#     re-merge of that branch a no-op and is a well-known trap;
#   - avoids rewriting a branch two production apps deploy from.
#
# `git read-tree -u --reset` is what does the work: it matches the target tree
# exactly, INCLUDING deleting files added since. `git checkout <ref> -- .` does
# not delete, which would leave the new endpoints in place against old code.
#
# WHAT THIS SCRIPT DOES NOT DO, and you must:
#
#   1. APP_VERSION on BOTH apps. The code fallback only applies when the app
#      setting is missing, so until you change it the header keeps claiming the
#      version you just rolled away from. Commands are printed at the end.
#   2. The endpoint cache. impactmgr.endpoint_cache holds built payloads for 26
#      hours (shared_code/endpoint_cache.py). A payload written by the newer
#      code can be served to the older code under the same key. Clear it. SQL is
#      printed at the end.
#   3. Tell people. A rollback changes every tab back.
#
set -euo pipefail

REF="pre-3.0.0"
CONFIRM="no"
for arg in "$@"; do
  case "$arg" in
    --yes) CONFIRM="yes" ;;
    -*)    echo "unknown flag: $arg" >&2; exit 2 ;;
    *)     REF="$arg" ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"
SELF="tools/rollback-release.sh"

git rev-parse --verify --quiet "$REF^{commit}" >/dev/null || {
  echo "ERROR: '$REF' is not a commit in this repo." >&2; exit 1; }

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] || {
  echo "ERROR: on '$BRANCH'. Production deploys from main; roll back there." >&2; exit 1; }

[ -z "$(git status --porcelain)" ] || {
  echo "ERROR: working tree is dirty. Commit or stash first — this script" >&2
  echo "       rewrites the index and worktree to match '$REF'." >&2; exit 1; }

FROM="$(git rev-parse --short HEAD)"
TO="$(git rev-parse --short "$REF")"
echo "rolling main back:  $FROM  ->  tree of $REF ($TO)"
echo
echo "version now:    $(grep -oE "APP_VERSION', '[0-9][0-9.]*'" api/GetConfig/__init__.py | grep -oE '[0-9][0-9.]*')"
echo "version after:  $(git show "$REF:api/GetConfig/__init__.py" | grep -oE "APP_VERSION', '[0-9][0-9.]*'" | grep -oE '[0-9][0-9.]*')"
echo
echo "files changed:"
git diff --stat "HEAD..$REF" | tail -25
echo
D=$(git diff --diff-filter=D --name-only "HEAD..$REF" | grep -v "^$SELF$" | wc -l | tr -d ' ')
echo "files the rollback DELETES: $D"
git diff --diff-filter=D --name-only "HEAD..$REF" | grep -v "^$SELF$" | sed 's/^/    /'
git diff --diff-filter=D --name-only "HEAD..$REF" | grep -q "^$SELF$" \
  && echo "    ($SELF is in the target's deletions but is kept, see below)"

if [ "$CONFIRM" != "yes" ]; then
  echo
  echo "DRY RUN. Nothing changed. Re-run with --yes to commit, then push."
  exit 0
fi

git read-tree -u --reset "$REF"

# Keep this script, whatever the target tree says. It postdates most release
# tags, so a plain tree restore deletes it -- leaving you rolled back with no
# tool to roll forward again. Rolling forward is this same script aimed at the
# newer commit, so it has to survive its own use.
git checkout "$FROM" -- "$SELF" 2>/dev/null || true
git add -A -- "$SELF" 2>/dev/null || true

git commit -q -m "rollback: restore the tree of $REF ($TO)

Production rolled back from $FROM. This commit's tree is byte-identical to
$REF; \`git diff $REF\` is empty.

A forward commit rather than a revert of the merge or a force-push: both apps
deploy main's tip, so this is the fastest way back, it leaves the rolled-back
work reachable, and it keeps \`git revert -m 1\` off a merge that may need to
be re-merged later.

Still to do by hand: set APP_VERSION on both Static Web Apps, and clear
impactmgr.endpoint_cache so no payload built by the newer code is served to
this one."

echo
echo "committed $(git rev-parse --short HEAD). Tree matches $REF: $([ -z "$(git diff "$REF" --stat)" ] && echo yes || echo NO)"
cat <<EOF

NEXT, in order:

  1. git push origin main

  2. Set the version on BOTH apps (the header lies until you do):

     for app in ghr-impact-manager ghr-nonmsp-impactmgr; do
       az staticwebapp appsettings set --name \$app \\
         --resource-group GHR_Azure_Resources \\
         --setting-names APP_VERSION=<the version printed above>
     done

  3. Clear the built-payload cache, or the old code may serve a payload the
     new code built (26h TTL):

     DELETE FROM impactmgr.endpoint_cache;

  4. Watch both deploys:  gh run list --branch main --limit 4

TO ROLL FORWARD AGAIN, this same script aimed the other way:

     tools/rollback-release.sh $FROM --yes

  (kept in the tree deliberately through the rollback, so it is still here.)
EOF
