#!/bin/sh
set -x
set -e
mydir=$(cd $(dirname $0); pwd)
token=$(cat $mydir/migration-oauth-key.txt)

try2() {
  "$@" || "$@"
}

try2 git -C mono/monorepo push --prune https://$token@github.com/llvm/llvm-project 'refs/heads/master:refs/heads/master' 'refs/heads/release/*:refs/heads/release/*' 'refs/tags/llvmorg-*:refs/tags/llvmorg-*'
try2 git -C mono/monorepo push --prune https://$token@github.com/llvm/llvm-project-legacy-branches 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'

for x in archive lnt test-suite www www-pubs zorg; do
  try2 git -C split/$x push --prune https://$token@github.com/llvm/llvm-$x 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'
done
