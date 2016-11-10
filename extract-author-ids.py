## Given the set of official LLVM git-svn repositories, and an llvm
## svn repository, generates a list of author email mappings.

import subprocess, re

rev_to_git_author = {}

def do_git():
#  for p in ["clang", "clang-tools-extra", "compiler-rt", "dragonegg", "libcxxabi", "libcxx", "lldb", "lld", "llgo", "llvm", "lnt", "polly", "test-suite"]:
  for p in ["clang", "clang-tools-extra", "compiler-rt", "dragonegg", "libcxxabi", "libcxx", "lldb", "lld", "llgo", "llvm", "lnt", "polly"]:
#  for p in ["clang", "llvm"]:
    git_log = subprocess.check_output(["git", "-C", "/d2/llvm-gits/%s" % p, "log"]).split('\n')
    author = None
    for l in git_log:
      if l.startswith("Author: "):
        author=l[len("Author: "):]
      elif l.startswith("    git-svn-id: "):
        assert author is not None
        rev = int(l.split("@")[1].split(' ')[0])

        old_author = rev_to_git_author.get(rev)
        if old_author and old_author[1] != author:
          print "Collision: %s vs %s at %s %s" % (old_author, author, p, rev)
        rev_to_git_author[rev] = (p, author)
        author = None

svn_authors = {}
def do_svn():
  svn_log = subprocess.check_output(["svn", "log", "file:///d2/llvm-svn"]).split("\n")
#  svn_log = open("/tmp/svn-log").read().split("\n")
  i = 0
  while True:
    assert svn_log[i] == "------------------------------------------------------------------------"
    i = i + 1
    if i == len(svn_log) - 1:
      break
    l = svn_log[i]
    match = re.match('^r([0-9]*) \| (.*) \| .* \| ([0-9]*) lines?', l)
    if not match:
      print i, len(svn_log)
      print l
      assert match
    rev = int(match.group(1))
    author = match.group(2)
    numlines = int(match.group(3))

    svn_authors[rev] = author
    i = i + numlines + 2

svn_to_git = {}
def do_authors():
  for rev,author in sorted(svn_authors.iteritems()):
    git_author = rev_to_git_author.get(rev)
    if git_author:
      old_git_author = svn_to_git.get(author)
      if old_git_author and old_git_author[2] and git_author[1] != old_git_author[2]:
        print "SVN-Name-collision: %s : %s %s vs %s" % (author, rev, git_author, old_git_author)
        svn_to_git["%s@%s" % (author,old_git_author[0])] = old_git_author
      svn_to_git[author] = (rev, git_author[0], git_author[1])
    else:
      if not svn_to_git.get(author):
        svn_to_git[author] = (rev, None, None)

def print_authors():
  for svn_author, (rev, p, git_author) in sorted(svn_to_git.iteritems()):
    print "%s = %s" % (svn_author, git_author)


do_git()
do_svn()
do_authors()
print_authors()
