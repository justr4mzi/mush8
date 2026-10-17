#!/bin/bash
# Push the text logs in ./out to the ci-logs branch, so they can be read
# without the GitHub web UI. Usage: publish-logs.sh <name>
set -u
NAME="$1"
[ -d out ] || exit 0
URL="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}"
D="$RUNNER_TEMP/ci-logs"
rm -rf "$D"
if ! git clone -q --depth 1 -b ci-logs "$URL" "$D" 2>/dev/null; then
  mkdir -p "$D" && cd "$D" && git init -q && git checkout -q -b ci-logs && git remote add origin "$URL" && cd - >/dev/null
fi
T="$D/$NAME/$GITHUB_RUN_ID"
mkdir -p "$T"
cp -R out/. "$T/"
cd "$D"
git add -A
git -c user.name=ci -c user.email=ci@localhost commit -qm "$NAME logs $GITHUB_RUN_ID" || exit 0
for i in 1 2 3 4 5; do
  git push -q origin ci-logs && exit 0
  git pull -q --rebase origin ci-logs || true
  sleep 3
done
