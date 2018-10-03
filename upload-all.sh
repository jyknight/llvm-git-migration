#!/bin/sh
set -x
set -e
mydir=$(cd $(dirname $0); pwd)
token=$(cat $mydir/migration-oauth-key.txt)

git -C mono/monorepo push --prune https://$token@github.com/llvm-git-prototype/llvm 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'

for x in archive lnt poolalloc safecode test-suite www www-pubs zorg; do
  git -C split/$x push --prune https://$token@github.com/llvm-git-prototype/$x 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'
done
