#!/usr/bin/python
#
# This tool takes a repository containing monorepo history, rewritten
# subproject fork histories (done by migrate-downstream-fork.py) along
# with the revmap produced by migrage-downstream-fork.py, an
# "umbrella" history consisting of submodule updates from subprojects
# and rewrites the umbrella history so that the submodule updates are
# "inlined" directly from the rewritten subproject histories.  The
# result is a history that interleaves rewritten subproject commits
# (zips them) according to the submodules updates, making it appear as
# if the commits were originally against the monorepo in the order
# implied by the umbrella history.
#
# Any non-LLVM submodules will be retained in their directories as
# they appear in the umbrella history.
#
# Usage:
#
# First, prepare a repository by following the instructions in
# migrate-downstream-fork.py.  Pass --revmap-out=$file to create a
# mapping from old downstream commits to new downstream commits.
#
# Then add umbrella history:
#   git remote add umbrella https://...
#
# Be sure to add the history from any non-llvm submodules:
#
#   for submodule in ${my_non_llvm_submodule_list[@]}; do
#     git remote add ${submodule} $(my_submodule_url ${submodule})
#   done
#
# Pull it all down:
#   git fetch --all
#
# Then, run this script:
#   zip-downstream-fork.py refs/remotes/umbrella --revmap-in=$file \
#                          --subdir=<dir>
#
# --subdir specified where to rewrite trees (directories and files)
# that are not part of a submodule.  Things such as top-level READMEs,
# build scripts, etc. will appear under <dir>.  This is to avoid
# possible conflicts with top-level entries in the upstream monorepo.
#
# With --revmap-out=$outfile the tool will dump a map from original
# umbrella commit hash to rewritten umbrella commit hash.
#
# TODO/Limitations:
#
# - The script requires a history with submodule updates.  It should
#   be fairly straightforward to enhance the script to take a revlist
#   directly, ordering the commits according to the revlist.  Such a
#   revlist could be generated from an umbrella repository or via
#   site-specific mechanisms.  This would be passed to
#   fast_filter_branch.py directly, rather than generating a list via
#   expand_ref_pattern(self.reflist) in Zipper.run as is currently
#   done.  Changes would need to be made to fast_filter_branch.py to
#   accept a revlist to process directly, bypassing its invocation of
#   git rev-list within do_filter.
#
# - The script assumes submodules for upstream projects in the
#   umbrella appear in the same places they do in the monorepo
#   (i.e. an llvm submodule exists at "llvm" in the umbrella, a clang
#   submodule exists at "clang" in the umbrella, and so on).
#
# - Submodule removal is not handled at all.  The subproject will
#   continue to exist though no updates to it will be made.  This
#   could by added by judicial use of fast_filter_branch.py's
#   TreeEntry.remove_entry.
#
# - Subproject tags are not rewritten.  Because the subproject commits
#   themselves are not rewritten, any downstream tags pointing to them
#   won't be updated to point to the zipped history.  We could provide
#   this capability if we updated the revmap entry for subproject
#   commits to point to the corresponding zipped commit during
#   filtering.
#
# - If a downstream commit merged in an upstream commit, parents for
#   the "inlined" submodule update are rewritten correctly, though the
#   history can look a bit strange as updates to multiple submodules
#   can create parents to history that is "already merged."  For
#   example:
#
#   *   (HEAD -> zip/master) Merge commit FOO from clang
#   |\
#   * \   Merge commit BAR from llvm
#   |\ \
#   | \ \
#   |  * |    Do commit BAR in llvm
#   |  |/
#   |  *      Do commit FOO in clang
#   |  |
#   *  |      Downstream llvm work
#   |  |
#      Monorepo
#
#   There's no real harm in this, it just looks strange.  A possible
#   enhancement for this script is to collapse submodule updates that
#   merge from upstream and have the result point to the most recent
#   upstream commit merged in.  However, this is difficult to do in
#   general, because subprojects might have been updated from upstream
#   at very different times and detecting a related set of submodule
#   updates is not straightforward.  Even a simple heuristic of
#   "collapse all submodule upstream updates between downstream
#   commits" won't always work, because it's possible that a
#   downstream commit was submodule-updated in the middle of someone
#   else updating all the subprojects from upstream.
#
import argparse
import fast_filter_branch
import os
import re
import subprocess
import sys

def expand_ref_pattern(patterns):
  return subprocess.check_output(
      ["git", "for-each-ref", "--format=%(refname)"] + patterns
  ).split("\n")[:-1]

class Zipper:
  """Destructively zip a submodule umbrella repository."""
  def __init__(self, new_upstream_prefix, revmap_in_file, revmap_out_file,
               reflist, debug, abort_bad_submodule, no_rewrite_commit_msg,
               subdir):
    if not new_upstream_prefix.endswith('/'):
      new_upstream_prefix = new_upstream_prefix + '/'

    self.new_upstream_prefix     = new_upstream_prefix
    self.revmap_in_file          = revmap_in_file
    self.revmap_out_file         = revmap_out_file
    self.reflist                 = reflist
    self.new_upstream_hashes     = set()
    self.added_submodules        = set()
    self.merged_upstream_parents = set()
    self.revap                   = {}
    self.dbg                     = debug
    self.prev_submodules         = []
    self.abort_bad_submodule     = abort_bad_submodule
    self.no_rewrite_commit_msg   = no_rewrite_commit_msg
    self.subdir                  = subdir

  def debug(self, msg):
    if self.dbg:
      print msg
      sys.stdout.flush

  def get_user_yes_no(self, msg):
    sys.stdout.flush
    done = False
    while not done:
      answer = raw_input(msg + " (y/n) ")
      answer = answer.strip()
      done = True
      if answer is not "y" and answer is not "n":
        done = False

    return answer

  def gather_upstream_commits(self):
    """Walk all refs under new_upstream_prefix and record hashes."""
    refs = expand_ref_pattern([self.new_upstream_prefix])

    if not refs:
      raise Exception("No refs matched new upstream prefix %s" % self.new_upstream_prefix)

    # Save the set of git hashes for the new monorepo.
    self.new_upstream_hashes = set(subprocess.check_output(['git', 'rev-list'] + refs).split('\n')[:-1])

  def find_submodules_in_entry(self, githash, tree, path):
    """Figure out which submodules/submodules commit an existing tree references.

    Returns [([submodule pathsegs], hash)], or [] if there are no submodule
    updates to submodules we care about.  Recurses on subentries.
    """

    subentries = tree.get_subentries(self.fm)

    submodules = []

    for name, e in subentries.iteritems():
      if e.mode == '160000':
        # A commit; this is a submodule gitlink.

        try:
          commit = self.fm.get_commit(e.githash)
        except:
          # It can happen that a submodule update refers to a commit
          # that no longer exists.  This is usually the result of user
          # error with a submodule update to a commit not reachable by
          # any branch in the subproject.  We almost always want to
          # skip these, but ask the user to make sure.  If they don't
          # want to skip it, then we really don't know what to do and
          # the user will have to fix things up and try again.
          print 'WARNING: No commit %s for submodule %s in commit %s' % (e.githash, name, githash)
          if self.abort_bad_submodule:
            raise Exception('No commit %s for submodule %s in commit %s' % (e.githash, name, githash))
          continue

        submodule_path = list(path)
        submodule_path.append(name)
        submodule_entry = (submodule_path, e.githash)
        submodules.append(submodule_entry)

      elif e.mode == '40000':
        subpath = list(path)
        subpath.append(name)
        submodules.extend(self.find_submodules_in_entry(githash, e, subpath))

    return submodules

  def find_submodules(self, commit, githash):
    """Figure out which submodules/submodule commits an existing commit references.

    Returns [([submodule pathsegs], hash)], or [] if there are no submodule
    updates to submodules we care about.  Recurses the tree structure.
    """

    return self.find_submodules_in_entry(githash, commit.get_tree_entry(), [])

  def clear_tree(self, tree):
    """Remove all entries from tree"""
    subentries = tree.get_subentries(self.fm).items()
    for name, entry in subentries:
      tree = tree.remove_entry(self.fm, name)

    return tree

  def rewrite_tree(self, tree, subdir):
    """Move the top-level entries under subdir"""

    if tree.mode == '40000':
      entries = tree.get_subentries(self.fm).copy()

      subtree = fast_filter_branch.TreeEntry('40000', sub_entries = entries)

      subtree.write_subentries(self.fm)

      tree = tree.add_entry(self.fm, subdir, subtree)

      for name, entry in tree.get_subentries(self.fm).items():
        if name is not subdir:
          tree = tree.remove_entry(self.fm, name)

      self.debug('NEWTREE:\n')
      for name, entry in tree.get_subentries(self.fm).items():
        self.debug('%s %s\n' % (name, str(entry)))

    return tree

  def zip_filter(self, fm, githash, commit, oldparents):
    """Rewrite an umbrella branch with interleaved commits

    These commits are assumed to be from an 'umbrella' repository
    which has a linear ordering of commits that update submodule
    links.  This routine rewrites such commits so that their content
    is that of the submodule commit(s).

    Each rewritten commit has a first parent of the previous rewritten
    umbrella commit.  If the commit added submodules, the parent list
    includes the rewritten commits of the added submodules.

    Given a revmap of rewritten commits and a ref to a linear order of
    commits that update submodule references to rewritten commits (an
    "umbrella" repository branch), create a map from each rewritten
    downstream commit to a list of new parents it should have to make
    it appear as if the commits had been interleaved in the monorepo
    as in the umbrella branch.  Any parent references to upstream
    commits will be left alone.  References to downstream commits will
    be changed to reflect the interleaved linear ordering in the
    umbrella history.
    """

    self.debug('--- commit %s' % githash)

    submodules = self.find_submodules(commit, githash)

    if not submodules:
      # No submodules imported yet.
      self.debug('No submodules yet - rewrite\n')
      newtree = self.rewrite_tree(commit.get_tree_entry(), self.subdir)
      newtree.write_subentries(self.fm)
      commit.treehash = newtree.githash
      return commit

    # The content of the commit should be the combination of the
    # content from the submodules.
    newtree = commit.get_tree_entry()

    # First, remove all submodule updates.  We don't want to rewrite
    # these under subdir.
    for pathsegs, oldhash in submodules:
      newtree = newtree.remove_path(self.fm, pathsegs)

    prev_submodules_map = {}

    if not self.no_rewrite_commit_msg:
      # Track the old hashes for submodules so we know which
      # submodules this commit updated below.
      for prev_submodule_pathsegs, prev_submodule_hash in self.prev_submodules:
        prev_submodules_map['/'.join(prev_submodule_pathsegs)] = prev_submodule_hash

    self.prev_submodules = submodules

    # Rewrite the non-submodule-update portions of the tree under
    # subdir.
    self.debug('Rewrite non-submodule entries\n')
    newtree = self.rewrite_tree(newtree, self.subdir)

    upstream_parents = []
    submodule_add_parents = []

    new_commit_msg = ''
    if self.no_rewrite_commit_msg:
      new_commit_msg = commit.msg

    # Import trees from commits pointed to by the submodules.  We
    # assume the trees should be placed in the same paths the
    # submodules appear.
    for pathsegs, oldhash in submodules:
      path='/'.join(pathsegs)
      self.debug('Found submodule (%s, %s)' % (path, oldhash))

      # Get the hash of the rewritten commit corresponding to the
      # submodule update.
      newhash = self.revmap.get(oldhash, oldhash)
      newcommit = self.fm.get_commit(newhash)
      self.debug('New hash: %s' % newhash)

      # We assume submodules for upstream projects in the umbrella
      # appear in the same places they do in the monorepo (i.e. an
      # llvm submodule exists at "llvm" in the umbrella, a clang
      # submodule exists at "clang" in the umbrella, and so on).
      submodule_tree = newcommit.get_tree_entry().get_path(self.fm, pathsegs)

      if not submodule_tree:
        # This submodule doesn't exist in the monorepo, add the
        # entire contents of the commit's tree.
        submodule_tree = newcommit.get_tree_entry()

      newtree = newtree.add_path(self.fm, pathsegs, submodule_tree)

      if not self.no_rewrite_commit_msg:
        if not path in prev_submodules_map or prev_submodules_map[path] != oldhash:
          if not new_commit_msg:
            new_commit_msg = newcommit.msg
          else:
            new_commit_msg += '\n' + newcommit.msg

      # Rewrite parents.  If this commit added a new submodule, add a
      # parent to the corresponding commit.  If one of the submodule
      # commits merged from upstream, add the upstream commit.
      if path not in self.added_submodules:
        self.debug('Merge new submodule %s' % path)
        submodule_add_parents.append(newhash)
        self.added_submodules.add(path)

      for parent in newcommit.parents:
        self.debug('Checking parent %s' % parent)
        if parent in self.new_upstream_hashes and not parent in self.merged_upstream_parents:
          self.debug('Merge upstream commit %s' % parent)
          upstream_parents.append(parent)
          self.merged_upstream_parents.add(parent)

    newtree.write_subentries(fm)
    commit.treehash = newtree.githash

    for name, e in newtree.get_subentries(fm).iteritems():
      self.debug('NEWTREE: %s %s' % (name, str(e)))

    commit.parents.extend(submodule_add_parents)
    commit.parents.extend(upstream_parents)
    commit.msg = new_commit_msg

    return commit

  def run(self):
    if not self.revmap_in_file:
      raise Exception("No revmap specified, use --revmap-in")

    if self.revmap_out_file:
      # Only supports output, not input
      try:
        os.remove(self.revmap_out_file)
      except OSError:
        pass

    print "Mapping commits..."
    self.revmap = dict((line.strip().split(' ') for line in file(self.revmap_in_file)))

    self.fm = fast_filter_branch.FilterManager()
    print "Getting upstream commits..."
    self.gather_upstream_commits()
    print "Done."

    print "Zipping commits..."
    fast_filter_branch.do_filter(commit_filter=self.zip_filter,
                                 filter_manager=self.fm,
                                 revmap_filename=self.revmap_out_file,
                                 reflist=expand_ref_pattern(self.reflist))
    self.fm.close()
    print "Done -- refs updated in-place."

if __name__=="__main__":
  parser = argparse.ArgumentParser(description="""
This tool zips up downstream commits created by migrate-downstream-fork.py
according to a set of commits assumed to be from an 'umbrella' repository.
The umbrella history is a series of commits that do submodule updates from
split-project git repositories.  Any commits without submodule modifications
are skipped.

The umbrella history is rewritten so that each commit appears to have
been done directly to the umbrella, instead of via a submodule update.
Merges from upstream monorepo commits are preserved.  The commit
message is replaced by the commit message(s) from the updated
submodule(s), unless --no-rewrite-commit-msg is given.

This tool DESTRUCTIVELY MODIFIES the umbrella branch it is run on!

Typical usage:
  # First, prepare a repository by following the instructions in
  # migrate-downstream-fork.py.  Pass --revmap-out=$file to create
  # a mapping from old downstream commits to new downstream commits.

  # Then add umbrella history:
  git remote add umbrella https://...

  # Be sure to add the history from any non-llvm submodules:
  for submodule in ${my_non_llvm_submodule_list[@]}; do
    git remote add ${submodule} $(my_submodule_url ${submodule})
  done

  # Pull it all down:
  git fetch --all

  # Then, run this script:
  zip-downstream-fork.py refs/remotes/umbrella --revmap-in=$file
""",
  formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--new-repo-prefix", metavar="REFNAME",
                      default="refs/remotes/new",
                      help="The prefix for all the refs of the new repository (default: %(default)s).")
  parser.add_argument("reflist", metavar="REFPATTERN",
                      help="Patterns of the references to convert.", nargs='*')
  parser.add_argument("--revmap-in", metavar="FILE", default=None)
  parser.add_argument("--revmap-out", metavar="FILE", default=None)
  parser.add_argument("--debug", help="Turn on debug output.", action="store_true")
  parser.add_argument("--abort-bad-submodule",
                      help="Abort on bad submodule updates.", action="store_true")
  parser.add_argument("--no-rewrite-commit-msg",
                      help="Don't rewrite the submodule update commit message with the merged commit message.", action="store_true")
  parser.add_argument("--subdir", metavar="DIR",
                      help="Subdirectory under which to write non-submodule trees")
  args = parser.parse_args()
  Zipper(args.new_repo_prefix, args.revmap_in, args.revmap_out, args.reflist,
         args.debug, args.abort_bad_submodule, args.no_rewrite_commit_msg,
         args.subdir).run()
