#!/bin/bash

set -x
set -u
set -e
set -o pipefail

mydir=$(cd $(dirname $0) && pwd)
SVNEXPORT=(svn-all-fast-export /d2/llvm-svn --only-note-toplevel-merges --rules "$mydir/llvm-svn2git.rules" --add-metadata)

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

initial_import() {
  # We insert some deletions into master to delete some historically
  # interesting, but abandoned projects nearer to where they had been
  # abandoned
  mkdir pristine

  # Run svn2git to make a repository
  cd pristine
  "${SVNEXPORT[@]}" --max-rev 26059
  delete_proj 1139395770 java
  "${SVNEXPORT[@]}" --max-rev 40406
  delete_proj 1185141186 stacker
  "${SVNEXPORT[@]}" --max-rev 41689
  delete_proj 1188853962 hlvm
  "${SVNEXPORT[@]}" --max-rev 219392
  delete_proj 1412844219 vmkit
  "${SVNEXPORT[@]}"
  cd ..

  # Clone it into another dir
  git clone --mirror pristine/monorepo monorepo

  cd monorepo

  # Disable gc.
  git config gc.auto 0

  # Run postprocessing steps...
  $mydir/llvm_filter.py
  # Delete backup refs from llvm_filter (note: keep the ones from fixup-tags)
  git for-each-ref --format="delete %(refname)" refs/original/ | git update-ref --stdin
  $mydir/fixup-tags.py

  # Now, repack the processed repository tightly, and mark the
  # resulting packfile as "keep", so future repacks won't touch it.
  git repack -adf --window=9999 --window-memory=1g
  git prune
  for x in objects/pack/*.pack; do
    echo 'Base filtered packfile' > "${x%.pack}.keep"
  done
  cd ..
  
  # Copy the tightly-compressed packfile from monorepo back to
  # pristine/monorepo (with .keep file):
  cp -l monorepo/objects/pack/pack-* pristine/monorepo/objects/pack/
  # And repack the *other* objects (that were filtered out in the
  # post-processing) in the pristine monorepo into another packfile.
  # (This reduces the space-wastage here a bit).
  git -C pristine/monorepo repack -adf
  for x in pristine/monorepo/objects/pack/*.pack; do
    echo 'Extra pristine objects packfile' > "${x%.pack}.keep"
  done

  touch initial_import_done
}

incremental_update() {
  # Handle incremental updates to an existing conversion.
  cd pristine
  "${SVNEXPORT[@]}"
  cd ..

  cd monorepo
  # We will temporarily borrow objects from the pristine repo for the
  # filtered repo, while we re-run the filter step.  (this basically
  # accomplishes "git fetch", except without actually copying objects)
  export GIT_ALTERNATE_OBJECT_DIRECTORIES=../pristine/monorepo/objects
  git for-each-ref --format 'delete %(refname)' | git update-ref --stdin
  git -C ../pristine/monorepo for-each-ref --format 'create %(refname) %(objectname)' | git update-ref --stdin

  # Now, re-run the filtering:
  $mydir/llvm_filter.py
  # Delete backup refs from llvm_filter, and from the previous run of fixup-tags.
  git for-each-ref --format="delete %(refname)" refs/original/ refs/pre-fixup-tags/ | git update-ref --stdin
  $mydir/fixup-tags.py
  # Note: we *do* keep the resulting fixup-tags refs here, so that we
  # preserve the referenced commit objects for the next iteration.

  # And, repack the new objects, which makes the repo self-contained again.
  git repack -adf
  unset GIT_ALTERNATE_OBJECT_DIRECTORIES
  cd ..
}

if [ -e initial_import_done ]; then
  incremental_update 2>&1 | tee -a llvm-svn2git-update.log
else
  initial_import 2>&1 | tee llvm-svn2git.log
fi


