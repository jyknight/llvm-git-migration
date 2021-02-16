#!/usr/bin/python

import argparse
import fast_filter_branch
import os
import re
import subprocess
import sys

svnrev_re=re.compile('^(?:llvm-svn: |llvm-svn=|git-svn-rev: )([0-9]+)\n\Z|^git-svn-id: https?://llvm.org/svn/llvm-project/([^/]*).*@([0-9]+) [0-9a-f-]*\n\Z', re.MULTILINE)

def expand_ref_pattern(patterns):
  return subprocess.check_output(
      ["git", "for-each-ref", "--format=%(refname)"] + patterns
  ).split("\n")[:-1]

class Migrator:
  """Destructively migrate an existing repository to the new monorepo layout."""
  def __init__(self, new_upstream_prefix, old_upstream_prefix, revmap_file, reflist, old_upstream_notes_ref, source_kind):
    if not new_upstream_prefix.endswith('/'):
      new_upstream_prefix = new_upstream_prefix + '/'
    if not old_upstream_prefix.endswith('/'):
      old_upstream_prefix = old_upstream_prefix + '/'

    self.new_upstream_prefix = new_upstream_prefix
    self.old_upstream_prefix = old_upstream_prefix
    self.revmap_file = revmap_file
    self.base_svn_mapping = {}
    self.svn_to_newrev = {}
    self.reflist = reflist
    self.old_upstream_notes_ref = old_upstream_notes_ref
    self.source_kind = source_kind
    self.notes_tree = None

  def find_svnrev(self, rev):
    """Figure out what svn revision an existing commit is.

    Returns (svnrev, subproject), or (None, None) if it's not from an svn
    commit.
    """
    c = self.fm.get_commit(rev)

    re_match = svnrev_re.search(c.msg)
    if not re_match and self.notes_tree is not None:
      # If no match in the commit message, see if we can match it in the git note.
      notes_blob = self.notes_tree.get_path(self.fm, [rev[0:2], rev[2:4], rev[4:]])
      if notes_blob is not None:
        note_msg = self.fm.get_blob(notes_blob.githash)
        re_match = svnrev_re.search(note_msg)

    if not re_match:
      return None, None
    if re_match.group(1) is not None:
      return int(re_match.group(1)), None
    else:
      subproject = re_match.group(2)
      if subproject == 'cfe':
        subproject = 'clang'
      return int(re_match.group(3)), subproject

  def detect_new_svn_revisions(self):
    """Walk all refs under new_upstream_prefix, and find their svn revisions."""
    refs = expand_ref_pattern([self.new_upstream_prefix])

    if not refs:
      raise Exception("No refs matched new upstream prefix %s" % self.new_upstream_prefix)

    # Save the set of git hashes for the new monorepo.
    self.newrev_set = set(subprocess.check_output(['git', 'rev-list'] + refs).split('\n')[:-1])

    # Now, store a map from the svn revision number to the new git commit, and
    # from the git commit to the svn rev.
    for rev in self.newrev_set:
      svnrev, subproject = self.find_svnrev(rev)
      if svnrev is not None:
        if subproject is not None:
          raise Exception("Did not expect to find non-monorepo commit %s in upstream prefixes." % rev)
        self.svn_to_newrev[svnrev] = rev
        self.base_svn_mapping[rev] = (svnrev, subproject)

    if not self.svn_to_newrev:
      raise Exception("Couldn't find any svn revisions in upstream prefix?")

  def detect_old_svn_revisions(self):
    "Walk all refs under old_upstream_prefix, and find their svn revisions."""
    refs = expand_ref_pattern([self.old_upstream_prefix])

    if not refs:
      raise Exception("No refs matched old upstream prefix %s" % self.old_upstream_prefix)

    subproject_set = set()

    self.oldrev_set = set(subprocess.check_output(['git', 'rev-list'] + refs).split('\n')[:-1])
    for rev in self.oldrev_set:
      svnrev, subproject = self.find_svnrev(rev)
      if svnrev is not None:
        self.base_svn_mapping[rev] = (svnrev, subproject)
        subproject_set.add(subproject)

    # Now, set the source kind, depending on the source repositories.
    if self.source_kind == 'autodetect':
      split_repos = ['lnt', 'test-suite', 'www', 'www-pubs', 'zorg']
      if None in subproject_set:
        if len(subproject_set) != 1:
          raise Exception("Can't autodetect source-kind: old refs include both monorepo and multirepo commits? (Found %r)" % (subproject_set,))
        self.source_kind = "monorepo"
      elif subproject_set.intersection(split_repos):
        if len(subproject_set) != 1:
          raise Exception("Can't autodetect source-kind: old refs include more than one project, including one of the split repositories."
                          " (Found %r, which includes one of %r)." %
                          (subproject_set, split_repos))
        self.source_kind = "auxilliary"
      else:
        self.source_kind = "merge-split"

  def commit_filter(self, fm, githash, commit, oldparents):
    """Do the real filtering work..."""
    # If the commit is a new upstream commit, leave it alone
    if githash in self.newrev_set:
      return commit

    # Translate old upstream commit into new upstream commit
    if githash in self.oldrev_set:
      if githash in self.base_svn_mapping:
        svnrev = self.base_svn_mapping[githash][0]
        if svnrev in self.svn_to_newrev:
          return self.svn_to_newrev[svnrev]
        else:
          return commit

    # OK -- NOT an upstream commit.

    if self.source_kind == "auxilliary":
      # For auxilliary repositories, we don't need to change the tree at all, so
      # just return here.
      return commit

    # We need to move the tree under the correct subdir, and preserve everything
    # outside that subdir.  The tricky part is figuring out *which* parent to
    # get the rest of the tree (other than the named subproject) from, in case
    # of a merge.
    if not oldparents:
      raise Exception("Unexpected root commit %s" % githash)

    parent_svn_info = [self.base_svn_mapping[p] for p in oldparents]
    subproject = parent_svn_info[0][1]
    for p in parent_svn_info:
      if p[1] != subproject:
        raise Exception("Commit %s has parents from different subprojects? (%s vs %s)" %
                        githash, subproject, p[1])
    candidate_upstreams = [self.svn_to_newrev[p[0]] for p in parent_svn_info]

    if len(candidate_upstreams) > 1:
      # Filter multiple parents to only include the independent heads
      candidate_upstreams = subprocess.check_output(
          ["git", "merge-base", "--independent"] + candidate_upstreams
      ).split("\n")[:-1]

    if len(candidate_upstreams) != 1:
      # Still have multiple...give up.
      #
      # TODO: Could have some heuristics here, or manual mappings. E.g. if
      # you merge the release_60 branch, and later merge the release_70
      # branch, those do not have any ancestor relationship, but one might
      # assume that the resulting tree should be the contents of release_70.
      raise Exception("Could not determine new tree for merge commit %s -- multiple independent svn commit heads found: %r" %
                      (githash, candidate_upstreams))

    svnancestor = candidate_upstreams[0]
    svntree = fm.get_commit(svnancestor).get_tree_entry()

    if self.source_kind == "monorepo":
      # Monorepo conversion mode -- remove dragonegg and klee, add README.md
      newtree = commit.get_tree_entry()
      if newtree.get_path(fm, ['README.md']) is None:
        readme = svntree.get_path(fm, ['README.md'])
        if readme is not None:
          newtree.add_entry(fm, 'README.md', readme)
      for dir_to_remove in ('klee', 'dragonegg'):
        dir_to_remove_content = newtree.get_path(fm, [dir_to_remove])
        if dir_to_remove_content is not None:
          # Verify that the dir to be removed is unmodified.
          for parent in oldparents:
            if fm.get_commit(parent).get_tree_entry().get_path(fm, [dir_to_remove]) == dir_to_remove_content:
              break
          else:
            raise Exception("Fork of monorepo has modified the %r subdir, but that is not present in new monorepo." % (dir_to_remove,))
          newtree.remove_entry(fm, dir_to_remove)
    else:
      # Merge mode -- the primary purpose of this script: copy upstream tree,
      # and replace the relevant subtree.
      if subproject is None:
        raise Exception("Expected to find a subproject when in merge mode!")

      oldtree = commit.get_tree_entry()
      newtree = svntree.add_entry(fm, subproject, oldtree)
      newtree.write_subentries(fm)
      commit.treehash = newtree.githash

    self.base_svn_mapping[githash] = (self.base_svn_mapping[svnancestor][0], subproject)
    return commit

  def run(self):
    if self.revmap_file:
      # Only supports output, not input
      try:
        os.remove(self.revmap_file)
      except OSError:
        pass

    self.fm = fast_filter_branch.FilterManager()
    if self.old_upstream_notes_ref is not None:
      notes_commit = self.fm.get_commit(self.old_upstream_notes_ref)
      self.notes_tree = notes_commit.get_tree_entry()

    print "Detecting new svn revisions..."
    self.detect_new_svn_revisions()
    print "Detecting old svn revisions..."
    self.detect_old_svn_revisions()
    print "Done."
    print "Using conversion mode: %s (%s)." % (
        self.source_kind,
        {'monorepo' : 'Monorepo to monorepo',
         'merge-split': 'Split repositories to monorepo',
         'auxilliary': 'Auxilliary repo'}[self.source_kind],)

    print "Filtering commits..."
    fast_filter_branch.do_filter(commit_filter=self.commit_filter,
                                 filter_manager=self.fm,
                                 revmap_filename=self.revmap_file,
                                 reflist=expand_ref_pattern(self.reflist))
    self.fm.close()
    print "Done -- refs updated in-place."

if __name__=="__main__":
  parser = argparse.ArgumentParser(description="""

This tool assists with migrating a fork of a previous llvm git repository into
the new monorepo.

It supports taking an existing subproject-repository, with private commits and
merges, and translates each such commit as if had been committed on top of the
monorepo. The parent hashes will be modified to match.

It also supports migrating a fork of an old monorepo to the new monorepo, or
from forks of auxilliary projects (test-suite, lnt, zorg) to the new versions of
those.

This tool DESTRUCTIVELY MODIFIES the repository it is run on -- please always
run on a fresh clone!

Typical usage:
  # First, prepare a repository:
  mkdir myrepo-migrate && cd myrepo-migrate && git init

  git remote add --no-tags new https://github.com/llvm-git-prototype/llvm.git

  # And for every project you want to migrate, also fetch the svn mirror it was
  # based on...
  for x in llvm lld clang {{etc...}}; do
    git remote add --no-tags old/$x https://github.com/llvm-mirror/$x
    git remote add myrepo/$x https://.../$x
  done

  git fetch --all

  # Then, run this script:
  migrate-downstream-fork.py refs/remotes/myrepo refs/tags


For migrating a fork of test-suite (similarly lnt zorg etc):
  mkdir myrepo-migrate && cd myrepo-migrate && git init

  # Get the new and old upstreams.
  git remote add --no-tags new https://github.com/llvm-git-prototype/test-suite.git
  git remote add --no-tags old https://github.com/llvm-mirror/test-suite

  # Get your local repository refs and tags.
  git remote add myrepo/test-suite https://.../test-suite

  git fetch --all

  # Then, run this script:
  migrate-downstream-fork.py refs/remotes/myrepo refs/tags


For migrating from the old monorepo to the new monorepo:
  mkdir myrepo-migrate && cd myrepo-migrate && git init

  # Get the new and old upstreams.
  git remote add --no-tags new https://github.com/llvm-git-prototype/llvm.git
  git remote add --no-tags old https://github.com/llvm-project/llvm-project-20170507
  # Need notes as well
  git config --add remote.old.fetch "+refs/notes/commits:refs/notes/commits"

  # Get your local repository refs and tags.
  git remote add myrepo/llvm-project https://.../llvm-project

  git fetch --all

  # Then, run this script:
  migrate-downstream-fork.py --old-repo-notes=refs/notes/commits refs/remotes/myrepo refs/tags

""",
  formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--new-repo-prefix", metavar="REFPATTERN", default="refs/remotes/new",
                      help="The prefix for all the refs of the new repository (default: %(default)s).")
  parser.add_argument("--old-repo-prefix", metavar="REFPATTERN", default="refs/remotes/old",
                      help="The prefix for all the refs of the old repository/repositories (default: %(default)s).")
  parser.add_argument('--old-repo-notes', metavar="REF", default=None,
                      help="Additionally check for svn revision numbers in the given notes ref.")
  parser.add_argument('--source-kind', choices=["merge-split", "monorepo", "auxilliary", "autodetect"], default="autodetect",
                      help="What kind of old repository you have (default: autodetect)")
  parser.add_argument("reflist", metavar="REFPATTERN", help="Patterns of the references to convert.", nargs='+')
  parser.add_argument("--revmap-out", metavar="FILE", default=None)
  args = parser.parse_args()

  Migrator(args.new_repo_prefix, args.old_repo_prefix, args.revmap_out, args.reflist, args.old_repo_notes, args.source_kind).run()
