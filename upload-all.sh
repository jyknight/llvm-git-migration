#!/bin/sh
set -x
set -e
mydir=$(cd $(dirname $0); pwd)
token=$(cat $mydir/migration-oauth-key.txt)

try2() {
  "$@" || "$@"
}

# We want to exclude the refs pushed to the main repo in the legacy-branches
# repo, but git doesn't support negated refs in git push refspecs.  So, copy
# them to another dir, and push from there.
git -C mono/monorepo for-each-ref --format="delete %(refname)" refs/for-legacy-push | git -C mono/monorepo update-ref --stdin
git -C mono/monorepo for-each-ref --format="create refs/for-legacy-push/%(refname:lstrip=1) %(refname)" refs/heads refs/tags |
  grep -Ev ' (refs/heads/(master|release/.*)|refs/tags/llvmorg-.*)$' |
  git -C mono/monorepo update-ref --stdin

# Now, push everything
try2 git -C mono/monorepo push --prune https://$token@github.com/llvm/llvm-project 'refs/heads/master:refs/heads/master' 'refs/heads/release/*:refs/heads/release/*' 'refs/tags/llvmorg-*:refs/tags/llvmorg-*'
try2 git -C mono/monorepo push --prune https://$token@github.com/llvm/llvm-project-legacy-branches 'refs/for-legacy-push/heads/*:refs/heads/*' 'refs/for-legacy-push/tags/*:refs/tags/*'

for x in archive lnt test-suite www www-pubs zorg; do
  try2 git -C split/$x push --prune https://$token@github.com/llvm/llvm-$x 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'
done

git -C mono/monorepo for-each-ref --format="delete %(refname)" refs/for-legacy-push | git -C mono/monorepo update-ref --stdin
