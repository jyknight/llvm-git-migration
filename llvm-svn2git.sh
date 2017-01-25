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
  last_mark=$(awk '/progress SVN r[0-9]* branch refs\/pristine\/master = / { MARK=$7} END { print MARK }' log-monorepo)
git -C monorepo fast-import --import-marks=marks-monorepo --export-marks=marks-monorepo --quiet <<EOF
commit refs/pristine/master
mark $last_mark
author SVN to Git Conversion <nobody@llvm.org> $1 +0000
committer SVN to Git Conversion <nobody@llvm.org> $1 +0000
data ${#msg}
$msg
from refs/pristine/master^0
D $proj
EOF
}

monorepo_filter_steps() {
  cd monorepo
  # Clear and reset refs to filter
  git for-each-ref --format="delete %(refname)" refs/heads refs/tags | git update-ref --stdin
  set +x
  eval "$(git for-each-ref --shell --format='refname=%(refname); echo create "refs/heads/${refname#refs/pristine/}" "${refname}"' refs/pristine)" | git update-ref --stdin
  set -x
  # Run the filter
  $mydir/llvm_filter.py $mydir/author-ids.conf
  $mydir/fixup-tags.py
  cd ..
}

initial_svn2git() {
  # We insert some deletions into master to delete some historically
  # interesting, but abandoned projects nearer to where they had been
  # abandoned

  ## "${SVNEXPORT[@]}" --max-rev 26059
  ## delete_proj 1139395770 java
  ## "${SVNEXPORT[@]}" --max-rev 37801

  # Run svn2git to make a repository
  "${SVNEXPORT[@]}" --max-rev 26059
  delete_proj 1139395770 java
  "${SVNEXPORT[@]}" --max-rev 40406
  delete_proj 1185141186 stacker
  "${SVNEXPORT[@]}" --max-rev 41689
  delete_proj 1188853962 hlvm
  "${SVNEXPORT[@]}" --max-rev 219392
  delete_proj 1412844219 vmkit
  "${SVNEXPORT[@]}"

  git clone --mirror monorepo monorepo-backup
  rsync -av monorepo/ monorepo-backup/
  touch initial_svn2git_done
}

initial_import() {
  if [ -e initial_svn2git_done ]; then
    rsync -av --delete monorepo-backup/ monorepo/
  else
    initial_svn2git
  fi

  # Disable gc -- we'll do it manually
  git -C monorepo config gc.auto 0

  # Insert fixed objects into the repository, for the llvm_filter.py
  # script to use later on.
  for x in $mydir/fixed-files/*; do
    git -C monorepo hash-object -w $x
  done

  # Run postprocessing steps...
  monorepo_filter_steps

  # Now, repack the *final* bits of the repository tightly, and mark
  # the resulting packfile as "keep", so future repacks won't touch
  # it.
  rm -rf monorepo-repack
  git clone --mirror monorepo monorepo-repack
  cd monorepo-repack
  git for-each-ref --format="delete %(refname)" refs/pristine/ refs/pre-fixup-tags | git update-ref --stdin
  git repack -adf --window=9999 --window-memory=1g
  git prune
  for x in objects/pack/pack-*.pack; do
    touch "${x%.pack}.keep"
  done
  cd ..

  # Copy the tightly-compressed packfile from monorepo-repack back to
  # monorepo (with .keep file):
  cp -l monorepo-repack/objects/pack/pack-* monorepo/objects/pack/
  # And repack the *other* objects (the ones that get filtered out in the
  # post-processing) in the pristine monorepo into another packfile.
  git -C monorepo repack -adf
  for x in monorepo/objects/pack/pack-*.pack; do
    touch "${x%.pack}.keep"
  done

  touch initial_import_done
}

incremental_update() {
  # Handle incremental updates to an existing conversion.
  touch monorepo/timestamp-check
  "${SVNEXPORT[@]}"

  # And rerun filtering
  monorepo_filter_steps

  # ...then, because git-fast-import generates very-badly-packed
  # packfiles, unpack the objects from the new packs. (The very-newest
  # version of git-fast-import actually has a flag for this, but I'll
  # just do it manually here)
  cd monorepo
  for x in objects/pack/pack-*.pack; do
    if [[ $x -nt timestamp-check ]]; then
      rm -f "${x%.pack}.idx"
      git unpack-objects < "$x"
      rm -f "${x%.pack}".*
    fi
  done

  # And, run a gc to auto-repack as required.
  git gc --auto
}

if [ -e initial_import_done ]; then
  incremental_update 2>&1 | tee -a llvm-svn2git-update.log
else
  initial_import 2>&1 | tee -a llvm-svn2git.log
fi


