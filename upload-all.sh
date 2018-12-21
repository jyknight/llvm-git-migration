#!/bin/sh
set -x
set -e
mydir=$(cd $(dirname $0); pwd)
token=$(cat $mydir/migration-oauth-key.txt)

git -C mono/monorepo push --prune https://$token@github.com/llvm-git-prototype/llvm 'refs/heads/master:refs/heads/master' 'refs/heads/release/*:refs/heads/release/*' 'refs/tags/llvmorg-*:refs/tags/llvmorg-*'
git -C mono/monorepo push --prune https://$token@github.com/llvm-git-prototype/llvm-legacy-branches 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'

for x in archive lnt test-suite www www-pubs zorg; do
  git -C split/$x push --prune https://$token@github.com/llvm-git-prototype/$x 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'
done
