#!/bin/bash

set -x
set -u
set -e

mydir=$(dirname $0)

delete_proj() {
  time=$1
  proj=$2
  msg="Delete $proj project.
(Commit inserted retroactively during svn2git conversion)
"
  msglen=${#msg}
  # Replace the last revision's mark with the new commit
  last_mark=$(awk '/progress SVN r[0-9]* branch master = / { MARK=$7} END { print MARK }' log-monorepo)
git -C monorepo fast-import --import-marks=marks-monorepo --export-marks=marks-monorepo --quiet <<EOF
commit refs/heads/master
mark $last_mark
author svn2git <svn2git@localhost> $1 +0000
committer svn2git <svn2git@localhost> $1 +0000
data ${#msg}
$msg
from refs/heads/master^0
D $proj
EOF
}

{
  SVNEXPORT=(svn-all-fast-export /d2/llvm-svn --only-note-toplevel-merges --rules "$mydir/llvm-svn2git.rules" --add-metadata)

  # We insert some deletions into master to delete some historically
  # interesting, but abandoned projects nearer to where they had been
  # abandoned
  "${SVNEXPORT[@]}" --max-rev 26059
  delete_proj 1139395770 java
  "${SVNEXPORT[@]}" --max-rev 40406
  delete_proj 1185141186 stacker
  "${SVNEXPORT[@]}" --max-rev 41689
  delete_proj 1188853962 hlvm
  "${SVNEXPORT[@]}" --max-rev 219392
  delete_proj 1412844219 vmkit
  "${SVNEXPORT[@]}"
  git -C monorepo repack -dfA --window=9999 --window-memory=1g
} 2>&1 | tee llvm-svn2git.log

