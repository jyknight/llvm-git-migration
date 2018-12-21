#!/bin/bash

set -eux -o pipefail

mydir=$(cd $(dirname $0) && pwd)
SVNEXPORT=(svn-all-fast-export /d2/llvm-svn --keep-branch-commit --only-note-toplevel-merges --add-metadata --keep-branch-commit --rules)

delete_proj() {
  local repo=$1
  local time=$2
  local proj=$3
  local msg="Delete $proj project.
(Commit inserted retroactively during svn2git conversion)
"
  local msglen=${#msg}
  # Replace the last revision's mark with the new commit
  local last_mark=$(awk '/progress SVN r[0-9]* branch refs\/pristine\/master = / { MARK=$7} END { print MARK }' log-$repo)
git -C $repo fast-import --import-marks=marks-$repo --export-marks=marks-$repo --quiet <<EOF
commit refs/pristine/master
mark $last_mark
author SVN to Git Conversion <nobody@llvm.org> $time +0000
committer SVN to Git Conversion <nobody@llvm.org> $time +0000
data ${#msg}
$msg
from refs/pristine/master^0
D $proj
EOF
}

get_cleaned_branchname() {
  ref=$1
  name=${ref#refs/heads/release_}

  if [[ $name == 1 ]]; then
    echo "1.0.x"
  elif [[ $name =~ [0-3][0-9] ]]; then
    echo "${name:0:1}.${name:1:1}.x"
  elif [[ $name = *0 ]]; then
    echo "${name:0:-1}.x"
  else
    echo "${name}"
  fi
}

repo_filter_steps() {
  local repo=$1

  cd $repo
  # Clear and reset refs to filter
  git for-each-ref --format="delete %(refname)" refs/heads refs/tags refs/clean-heads refs/clean-tags | git update-ref --stdin
  { set +x; } 2>/dev/null
  eval "$(git for-each-ref --format='refname=%(refname); echo create "refs/heads/${refname#refs/pristine/}" "${refname}"' refs/pristine)" | git update-ref --stdin
  set -x
  # Run the filter
  $mydir/llvm_filter.py $repo $mydir/author-ids.conf
  $mydir/fixup-tags.py

  { set +x; } 2>/dev/null
  for ref in $(git for-each-ref --format="%(refname)" refs/heads/release_*); do
    newref="refs/heads/release/$(get_cleaned_branchname "$ref")"
    echo "renaming $ref to $newref"
    git update-ref "$newref" "$ref"
    git update-ref -d "$ref"
  done
  set -x
  cd ..
}

repack_initial_repo() {
  local repo=$1
  rm -rf "$repo-repack"
  git clone --mirror "$repo" "$repo-repack"

  cd "$repo-repack"
  git for-each-ref --format="delete %(refname)" refs/pristine/ refs/pre-fixup-tags | git update-ref --stdin
  git repack -adf --window=2500 --window-memory=1g
  git prune
  for x in objects/pack/pack-*.pack; do
    touch "${x%.pack}.keep"
  done
  cd ..

  # Copy the tightly-compressed packfile from $repo-repack back to
  # $repo (with .keep file):
  cp -l "$repo-repack"/objects/pack/pack-* "$repo/objects/pack/"
  # And repack the *other* objects (the ones that get filtered out in the
  # post-processing) in the pristine repo into another packfile.
  git -C "$repo" repack -adf
  for x in "$repo"/objects/pack/pack-*.pack; do
    touch "${x%.pack}.keep"
  done
  rm -rf "$repo-repack"
}

initial_svn2git() {
  if [[ "$IMPORTED_REPOS" =~ "monorepo" ]]; then
    # We insert some deletions into master to delete some historically
    # interesting, but abandoned projects nearer to where they had been
    # abandoned

    # Run svn2git to make a repository
    "${SVNEXPORT[@]}" "$RULESFILE" --max-rev 40406
    delete_proj monorepo 1185141186 stacker
    "${SVNEXPORT[@]}" "$RULESFILE"
  else
    "${SVNEXPORT[@]}" "$RULESFILE"
  fi
}

initial_import() {
  if [ -e initial_svn2git_done ]; then
    for repo in $IMPORTED_REPOS; do
      rm -rf $repo
      git clone --mirror $repo-backup $repo
      rsync -av --delete $repo-backup/ $repo/
    done
  else
    initial_svn2git

    for repo in $IMPORTED_REPOS; do
      git clone --mirror $repo $repo-backup
      rsync -av $repo/ $repo-backup/
    done
    touch initial_svn2git_done
  fi

  for repo in $IMPORTED_REPOS; do
    # Disable gc -- we'll do it manually
    git -C $repo config gc.auto 0
    # Force fast-import to not create packfiles
    git -C $repo config fastimport.unpackLimit 1000000000

    # Insert fixed objects into the repository, for the llvm_filter.py
    # script to use later on.
    if [[ $repo = monorepo ]]; then
      for x in $mydir/fixed-files/*; do
        git -C $repo hash-object -w $x
      done
    fi

    # Run postprocessing steps...
    repo_filter_steps $repo
  done
  # Now, repack the *final* bits of the repository tightly, and mark
  # the resulting packfile as "keep", so future repacks won't touch
  # it.
  for repo in $IMPORTED_REPOS; do
    repack_initial_repo $repo &
  done
  wait
}

incremental_update() {
  # Handle incremental updates to an existing conversion.
  "${SVNEXPORT[@]}" "$RULESFILE"

  for repo in $IMPORTED_REPOS; do
    # And rerun filtering
    repo_filter_steps $repo
    # And, run a gc to auto-repack as required.
    git -C $repo gc --auto
  done
}

main() {
  if [[ $# -lt 1 ]]; then
    echo "Syntax: $0 rulesfile" >&2
  fi

  RULESFILE=$1
  echo "Using rules file: $RULESFILE"
  IMPORTED_REPOS=$(sed -n 's/create repository \(.*\)/\1/p' < "$RULESFILE")

  if [ -e initial_import_done ]; then
    incremental_update 2>&1 | tee -a llvm-svn2git-update.log
  else
    initial_import 2>&1 | tee -a llvm-svn2git.log
    touch initial_import_done
  fi
}

main "$@"
