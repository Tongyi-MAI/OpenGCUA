#!/usr/bin/env bash
# Publish built files to an independent site branch without force-pushing.
set -euo pipefail

build_dir="$(cd "${1:?Usage: publish.sh BUILD_DIRECTORY}" && pwd)"
test -f "$build_dir/index.html"
test -f "$build_dir/contributing.html"
test -f "$build_dir/.nojekyll"
source_commit="$(git rev-parse HEAD)"
deploy_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/opengcua-site.XXXXXX")"
orphan_branch=""
cleanup() {
  git worktree remove --force "$deploy_dir" >/dev/null 2>&1 || true
  if [[ -n "$orphan_branch" ]]; then git branch -D "$orphan_branch" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

remote_ref="$(git ls-remote --heads origin refs/heads/site)"
if [[ -n "$remote_ref" ]]; then
  git fetch --no-tags --depth=1 origin refs/heads/site
  git worktree add --detach "$deploy_dir" FETCH_HEAD
else
  git worktree add --detach "$deploy_dir" HEAD
  orphan_branch="site-publish-$$"
  git -C "$deploy_dir" switch --orphan "$orphan_branch"
fi

git -C "$deploy_dir" rm -rf --ignore-unmatch -- .
cp -a "$build_dir/." "$deploy_dir/"
git -C "$deploy_dir" add --all
if git -C "$deploy_dir" diff --cached --quiet; then
  echo "Site output is unchanged."
else
  git -C "$deploy_dir" -c user.name='github-actions[bot]' \
    -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
    commit --no-gpg-sign -m "Deploy site from $source_commit"
  git -C "$deploy_dir" push origin HEAD:refs/heads/site
fi
