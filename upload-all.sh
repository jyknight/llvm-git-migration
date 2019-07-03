#!/bin/sh
set -x
set -e
mydir=$(cd $(dirname $0); pwd)
token=$(cat $mydir/migration-oauth-key.txt)

try2() {
  "$@" || "$@"
}

push_to_llvm_project() {
  try2 git -C mono/monorepo push $1 --prune https://$token@github.com/llvm/llvm-project 'refs/heads/master:refs/heads/master' 'refs/heads/release/*:refs/heads/release/*' 'refs/tags/llvmorg-*:refs/tags/llvmorg-*'
}

# We want to exclude the refs pushed to the main repo in the legacy-branches
# repo, but git doesn't support negated refs in git push refspecs.  So, copy
# them to another dir, and push from there.
git -C mono/monorepo for-each-ref --format="delete %(refname)" refs/for-legacy-push | git -C mono/monorepo update-ref --stdin
git -C mono/monorepo for-each-ref --format="create refs/for-legacy-push/%(refname:lstrip=1) %(refname)" refs/heads refs/tags |
  grep -Ev ' (refs/heads/(master|release/.*)|refs/tags/llvmorg-.*)$' |
  git -C mono/monorepo update-ref --stdin

# Push to llvm-project

# First push: If status checks are enabled this will be rejected, but having
# the push rejected will allow us to set the status checks on the commits.  The
# only other way to do this would be to first push to a temp branch, but that is
# more complicated as we would have to cleanup branches when done.
set +e
push_to_llvm_project
set -e

# Second push: Do a dry run and parse the output to determine which commits have not been
# pushed yet.
push_to_llvm_project -n 2>&1 |  grep -o '[0-9a-f]\+\.\.[0-9a-f]\+' | while read -r line; do
    git -C mono/monorepo rev-list $line | while read -r commit; do
      echo "Setting status check for: $commit"
      curl  -H "Authorization: token $token" https://api.github.com/repos/llvm/llvm-project/statuses/$commit  -X POST --data '{"state": "success", "context": "rebased" }'
    done
done

# Third push: Actually push the commits.
push_to_llvm_project

# Push legacy branches
try2 git -C mono/monorepo push --prune https://$token@github.com/llvm/llvm-project-legacy-branches 'refs/for-legacy-push/heads/*:refs/heads/*' 'refs/for-legacy-push/tags/*:refs/tags/*'

for x in archive lnt test-suite www www-pubs zorg; do
  try2 git -C split/$x push --prune https://$token@github.com/llvm/llvm-$x 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'
done

git -C mono/monorepo for-each-ref --format="delete %(refname)" refs/for-legacy-push | git -C mono/monorepo update-ref --stdin
