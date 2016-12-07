#!/usr/bin/python
import fast_filter_branch
import re

def main():
  file_changes = [
      ('/lldb/llvm.zip', None),
  ]

  svnrev_re=re.compile('^llvm-svn=[0-9]*\n', re.MULTILINE)

  # TODO: do historical author mapping here.
  # TODO: translate "Patch by ..." in commit messages to authorship info
  # TODO(maybe?): translate revision numbers in commit messages to git hashes

  def msg_filter(msg):
    # Clean up svn2git cruft in commit messages.  Also deal with
    # extraneous trailing newlines, and add a note where there's no
    # commit message other than the added revision info.
    msg = re.sub('\n+svn path=[^\n]*; revision=([0-9]*)\n?$', '\n\nllvm-svn=\\1\n', msg)
    if msg.startswith("\n\nllvm-svn="):
      msg = '(no commit message)' + msg
    return msg

  def commit_filter(fm, commit):
    # Multiple commits are used to make each branch -- one for each
    # subproject -- so the branch creation is uglier than necessary
    # (deleting a bunch of files during the initial branch, then
    # re-adding them in the followup merge-commits). We want to
    # Collapse those commits into one.

    # This detects a merge commit with the same message, author, and
    # other-parents as its first-parent.
    def filtermsg(msg):
      return svnrev_re.sub('', msg)

    if len(commit.parents) > 1:
      parent = fm.get_commit(commit.parents[0])
      if (commit.author == parent.author and
          commit.committer == parent.committer and
          filtermsg(commit.msg) == filtermsg(parent.msg) and
          (commit.parents[1:] == parent.parents or
           commit.parents[1:] == parent.parents[1:])):
        # The parent commit looks similar, so merge it.  (preserve the
        # revision number from its commit message, though).
        commit.msg += ''.join(svnrev_re.findall(parent.msg))
        commit.parents = parent.parents[:]
    return commit

  fast_filter_branch.do_filter(global_file_actions=file_changes,
                               msg_filter=msg_filter,
                               commit_filter=commit_filter,
                               revmap_filename="llvm_filter.revmap")

if __name__=="__main__":
  main()
