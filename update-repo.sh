#!/bin/bash

setup () {
# Setup trac
  trac-admin initenv llvm-trac
  cat >> llvm-trac/conf/trac.init <<EOF
[components]
tracopt.versioncontrol.svn.* = enabled
EOF

  trac-admin llvm-trac repository add llvm /d2/llvm-svn
}

sync () {
  svnsync sync file:///d2/llvm-svn
  trac-admin ~/Projects/llvm-git-migration/llvm-trac repository sync llvm
}

list-project () {
  "select * from node_change where path regexp '^llvm/branches/[^/]*$' order by path,rev;"
}

sync
