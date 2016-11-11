#!/bin/bash

set -x
set -u
set -e

delete_proj() {
  time=$1
  proj=$2
  msg="Delete $proj project.
(Commit inserted retroactively during svn2git conversion)
"
  msglen=${#msg}

git -C monorepo fast-import --quiet <<EOF
commit refs/heads/master
from refs/heads/master^0
author svn2git <svn2git@localhost> $1 +0000
committer svn2git <svn2git@localhost> $1 +0000
data ${#msg}
$msg
D $proj
EOF
}

{
  SVNEXPORT=svn-all-fast-export /d2/llvm-svn --rules llvm-svn2git.rules --add-metadata

  # We insert some deletions into master to delete some historically
  # interesting, but abandoned projects nearer to where they had been
  # abandoned
  $SVNEXPORT --max-rev 26059
  delete_proj 1139395770 java
  $SVNEXPORT --max-rev 40406
  delete_proj 1185141186 stacker
  $SVNEXPORT --max-rev 41689
  delete_proj 1188853962 hlvm
  $SVNEXPORT --max-rev 219392
  delete_proj 1412844219 vmkit
  $SVNEXPORT
} 2>&1 | tee llvm-svn2git.log

