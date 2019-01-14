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
# On the rewriting of trees:
#
# If a downstream commit merged in an upstream commit, parents for the
# "inlined" submodule update are rewritten correctly.  The tool takes
# care to preserve the proper history for upstream monorepo bits that
# do not participate in the submodule process.  For example, say the
# umbrella history looks like this:
#
#   *   (HEAD -> umbrella/master) Update submodule clang to FOO
#   |
#   *   Update submodule llvm to BAR
#   |
#   |  *   (HEAD -> llvm/local) Do commit BAR in llvm
#   |  |
#   |  |     *   (HEAD -> clang/local) Do commit FOO in clang
#   |  |     |
#   *  |     |        Downstream umbrella work
#   |  |     |
#     llvm  clang
#
# The umbrella history updates submodules from commits in local copies
# of llvm and clang.  Note that the llvm and clang histories have not
# yet been rewritten against the monorepo.
#
# Let's also say when the local llvm and clang branches are rewritten
# against the monorepo (by migrate-downstream-fork.py), it looks
# something like this:
#
#   *   (HEAD -> monorepo/master) Do commit XYZZY in lld
#   |
#   |  *   (HEAD -> monorepo-llvm/local) Do commit BAR in llvm
#   |  |
#   | /
#   |/
#   *   Do commit BAZ in compiler-rt
#   |
#   |  *   (HEAD -> monorepo-clang/local) Do commit FOO in clang
#   |  |
#   | /
#   |/
#   *   Do commit QUX in compiler-rt
#   |
#
# The commits from compiler-rt come from upstream (no local work
# exists for compiler-rt) but commits BAR and BAZ exist in local
# histories for llvm and compiler-rt which were rewritten against the
# upstream monorepo (i.e. they are in branches off monorepo/master or
# some other point).
#
# A naive processing of parents would leave us with something like
# this in the zipped history:
#
#   *   (HEAD -> monorepo/master) Do commit XYZZY in lld
#   |
#   |  *   (HEAD -> zip/master) Do commit FOO in clang
#   |  |\
#   |  * \   Do commit BAR in llvm
#   | /   |
#   |/    |
#   *     |   Do commit BAZ in compiler-rt
#   |    /
#   |   /
#   |  /
#   | /
#   |/
#   *   Do commit QUX in compiler-rt
#   |
#
# Not only is the edge from compiler-rt/QUX to zip/master redundant
# (it was supposedly merged along with compiler-rt/BAZ), the tree from
# compiler-rt could be written incorrectly, resulting in
# compiler-rt/QUX at zip/master rather than the proper
# compiler-rt/BAZ.  This is because monorepo-clang/FOO incorpates the
# tree from compiler-rt/QUX
#
# The script attempts to get this right by tracking the most recent
# merge-base from the monorepo along each zipped branch.  If a
# submodule update brings in an older tree from the monorepo, that
# tree is discarded in favor of the merge-base.  Otherwise the
# merge-base is updated to point to the new tree.  This means that the
# script assumes there is a total topological ordering among upstream
# commits brought in via submodule updates.  For example, the script
# will abort if trying to create a history like this:
#
#         *  (HEAD -> zip/master)
#        /|
#       / |
#      *  |  (HEAD -> llvm/local)
#     /   |
#    /    |
#   *     |  (HEAD -> monorepo/master
#   |     |
#   |     *  (HEAD -> clang/local)
#   |    /
#   |   /
#   |  *  (HEAD -> monorepo/branch1)
#   | /
#   |/
#
#
# llvm/local and clang/local are based off divergent branches of the
# monorepo and there is no total topological order among them.  It is
# not clear which monorepo tree should be used for other subprojects
# (compiler-rt, etc.).  In this case the script aborts with an error
# indicating the commit would create such a merge point.
#
# Note that the content appearing in subprojects will always reflect
# the tree found in the commit pointed to by the corresponding
# submodule.  This means that some subprojects may appear "older" in
# the resulting tree.  In the example above, clang/FOO came from a
# topologically earlier commit than llvm/BAR and the clang sources
# will be older than that of any other clang commits that may appear
# between clang/FOO and llvm/BAR.  The script favors preserving
# submodule updates as they appeared in the umbrella history under the
# assumption that subprojects were merged from upstream in lockstep.
#
# TODO/Limitations:
#
# - The script requires a history with submodule updates.  It should
#   be fairly straightforward to enhance the script to take a revlist
#   directly, ordering the commits according to the revlist.  Such a
#   revlist could be generated from an umbrella history or via
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
# - Submodule removal is not handled at all.  A third-party subproject
#   will continue to exist though no updates to it will be made.  This
#   could by added by judicial use of fast_filter_branch.py's
#   TreeEntry.remove_entry.  For projects managed by upstream (clang,
#   llvm, etc.), if a commit doesn't include a submodule (because it
#   was removed), the subproject tree is taken from the upstream
#   monorepo tree just as it is for upstream projects not
#   participating in the umbrella history.
#
# - Subproject tags are not rewritten.  Because the subproject commits
#   themselves are not rewritten (only the commits in the umbrella
#   history are rewritten), any downstream tags pointing to them won't
#   be updated to point to the zipped history.  We could provide this
#   capability if we updated the revmap entry for subproject commits
#   to point to the corresponding zipped commit during filtering.
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
    self.umbrella_merge_base     = {}

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

  def is_ancestor(self, potential_ancestor, potential_descendent):
    return subprocess.call(["git", "merge-base", "--is-ancestor",
                            potential_ancestor, potential_descendent]) == 0

  def list_is_ancestor(self, potential_ancestors, potential_descendent):
    Result = True
    for potential_ancestor in potential_ancestors:
      if not self.is_ancestor(potential_ancestor, potential_descendent):
        Result = False
    return Result

  def get_latest_upstream_commit(self, githash, submodules, candidates):
    """Determine which of candidates has the upstream tree we want."""

    if not candidates:
      return None

    result, result_path = candidates[0]

    if len(candidates) == 1:
      return result

    for candidate, path in candidates[1:]:
      self.debug("%s %s is_ancestor %s %s\n" % (result_path, result, path, candidate))
      if self.is_ancestor(result, candidate):
        result, result_path = [candidate, path]  # Candidate is newer
      elif not self.is_ancestor(candidate, result):
        # Neither is an ancestor of the other.  This must be a case
        # where the umbrella repository has updates from two different
        # upstream branches.  This is highly unusual and probably
        # something has gone wrong.  Abort for now.
        errstr = "Commit %s has submodule updates from multiple branches (%s %s)?\n\n" % (githash, path, candidate)
        for pathsegs, oldhash in submodules:
          errpath = '/'.join(pathsegs)
          errstr += "%s %s\n" % (errpath, oldhash)

        raise Exception(errstr)

    self.debug("Using %s %s as merge-base\n" % (result_path, result))

    return result

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
    self.debug('%s\n' % commit.msg)

    submodules = self.find_submodules(commit, githash)

    if not submodules:
      # No submodules imported yet.
      self.debug('No submodules yet - rewrite\n')
      newtree = self.rewrite_tree(commit.get_tree_entry(), self.subdir)
      newtree.write_subentries(self.fm)
      commit.treehash = newtree.githash
      return commit

    # The content of the commit should be the combination of the
    # content from the submodules and elements from the monorepo tree
    # not updated by submodules.  The tricky part is figuring out
    # which monorepo tree that should be.
    #
    # Start by assuming our upstream tree will be from the previous
    # umbrella rewrite, if there was one.
    #
    umbrella_merge_bases = []  # Hashes of upstream merge-bases from
                               # umbrella commits.

    for op in oldparents:
      parent_merge_base = self.umbrella_merge_base[self.fm.get_mark(op)]
      if parent_merge_base:
        umbrella_merge_bases.append([parent_merge_base, None])

    prev_submodules_map = {}

    # Track the old hashes for submodules so we know which
    # submodules this commit updated below.
    for prev_submodule_pathsegs, prev_submodule_hash in self.prev_submodules:
      prev_submodules_map['/'.join(prev_submodule_pathsegs)] = prev_submodule_hash

    self.prev_submodules = submodules

    new_commit_msg = ''
    if self.no_rewrite_commit_msg:
      new_commit_msg = commit.msg

    submodule_hash = {}

    # For each submodule, get the corresponding monorepo-rewritten
    # commit.  Figure out which monorepo tree to use as the base for
    # the new zipped commit.  For each submodule commit, examine its
    # parents.  If it more than one parent, the other parents may be
    # from the upstream monorepo, which would represent a merge from
    # upstream history and a potential new merge-base for the current
    # zip history.
    #
    # Given all of the parents of all of the submodule commits,
    # determine which one has the most recent content from upstream
    # and use its tree as the base for the new commit.
    #
    commits_to_check = umbrella_merge_bases  # Hashes of candidate
                                             # commits for the base
                                             # upstream tree.
    for pathsegs, oldhash in submodules:
      path='/'.join(pathsegs)
      self.debug('Found submodule (%s, %s)' % (path, oldhash))

      # Get the hash of the monorepo-rewritten commit corresponding to
      # the submodule update.
      newhash = self.revmap.get(oldhash, oldhash)
      self.debug('New hash: %s' % newhash)
      submodule_hash[path] = newhash

      newcommit = self.fm.get_commit(newhash)
      for parent in newcommit.parents:
        parent_hash = self.fm.get_mark(parent)
        if parent_hash in self.new_upstream_hashes:
          # This submodule has an upstream parent.
          self.debug("Upstream parent %s\n" % parent_hash)
          commits_to_check.append([parent_hash, path])

    umbrella_merge_base_hash = self.get_latest_upstream_commit(githash,
                                                               submodules,
                                                               commits_to_check)

    # Record our choice so children can find it.
    self.umbrella_merge_base[githash] = umbrella_merge_base_hash

    newtree = commit.get_tree_entry()

    # First, remove all submodule updates.  We don't want to rewrite
    # these under subdir.
    for pathsegs, oldhash in submodules:
      newtree = newtree.remove_path(self.fm, pathsegs)

    # Rewrite the non-submodule-update portions of the tree under
    # subdir.
    self.debug('Rewrite non-submodule entries\n')
    newtree = self.rewrite_tree(newtree, self.subdir)

    # Write the umbrella merge-base into the tree.
    if umbrella_merge_base_hash:
      umbrella_merge_base_commit = self.fm.get_commit(umbrella_merge_base_hash)

      umbrella_merge_base_tree = umbrella_merge_base_commit.get_tree_entry()
      for name, entry in umbrella_merge_base_tree.get_subentries(self.fm).items():
        newtree.add_entry(self.fm, name, entry)

    # Import trees from commits pointed to by the submodules.  We
    # assume the trees should be placed in the same paths the
    # submodules appear.
    submodule_add_parents = []   # Parents due to a "submodule add" operation
    upstream_parents = []        # Parents due to merges from upstream
    for pathsegs, oldhash in submodules:
      path='/'.join(pathsegs)

      # Get the hash of the rewritten commit corresponding to the
      # submodule update.
      newhash = submodule_hash[path]
      newcommit = self.fm.get_commit(newhash)

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

      prev_submodule_hash = None
      if path in prev_submodules_map:
        prev_submodule_hash = prev_submodules_map[path]

      if prev_submodule_hash != oldhash:
        if prev_submodule_hash:
          self.debug("Updated %s to %s (new %s)\n" % (path, oldhash, newhash))
        if not self.no_rewrite_commit_msg:
          if not new_commit_msg:
            new_commit_msg = newcommit.msg
          else:
            new_commit_msg += '\n' + newcommit.msg

      # Rewrite parents.  If this commit added a new submodule, add a
      # parent to the corresponding commit.
      if path not in self.added_submodules:
        self.debug('Add new submodule %s' % path)
        submodule_add_parents.append(newhash)
        self.added_submodules.add(path)

    # Add umbrella_merge_base as a parent if it's a descendent of all
    # previously merged upstream commits.
    if umbrella_merge_base_hash in self.new_upstream_hashes:
      if umbrella_merge_base_hash not in self.merged_upstream_parents:
        if self.list_is_ancestor(self.merged_upstream_parents, umbrella_merge_base_hash):
          # The new merge-base is newer than all previously-merged
          # upstream parents, so add an edge to it.
          self.debug('Add upstream merge parent %s' % umbrella_merge_base_hash)
          upstream_parents.append(umbrella_merge_base_hash)
          self.merged_upstream_parents.add(umbrella_merge_base_hash)

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
