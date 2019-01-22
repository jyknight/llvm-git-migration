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
# migrate-downstream-fork.py.  Pass --revmap-out=<file> to create a
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
#   zip-downstream-fork.py refs/remotes/umbrella --revmap-in=<file> \
#                          --subdir=<dir> [--submodule-map=<file>] \
#                          [--revmap-out=<file>]
#
# --subdir specified where to rewrite trees (directories and files)
# that are not part of a submodule.  Things such as top-level READMEs,
# build scripts, etc. will appear under <dir>.  This is to avoid
# possible conflicts with top-level entries in the upstream monorepo.
#
# The option --submodule-map=<file> is useful if your submodule layout
# is different from the monorepo layout.  By default the tool assumes
# project submodules exist at the top level of the umbrella history
# (e.g. in the same relative path as in the monorepo).  Use
# --submodule-map if your layout differs.  The map file should contain
# a mapping from submodule path to monorepo path, one mapping per
# line.  If a submodule path doesn't exist in the map, it is assumed
# to map to the same path in the monorepo.
#
# For example, if your layout looks like this:
#
# <root>
#   local-sources
#   upstream-sources
#     clang
#     compiler-rt
#     llvm
#
# then your submodule map file (submodule-map.txt) would look like
# this:
#
# upstream-sources/clang clang
# upstream-sources/compiler-rt compiler-rt
# upstream-sources/llvm llvm
#
# and you would invoke the tools as:
#
#   zip-downstream-fork.py refs/remotes/umbrella --revmap-in=$file \
#                          --subdir=<dir> \
#                          --submodule-map=submodule-map.txt
#
# Note that the mapping simply maps matching umbrella path names to
# monorepo paths.  There is no requirement that the umbrella path end
# with the same name as the monorepo path.  If your clang is imported
# under fizzbin, simply tell the mapper that:
#
# fizzbin clang
#
# The mapper can also move third-party submodules to new places:
#
# my-top-level-tool third-party/my-tool
#
# With --revmap-out=<file> the tool will dump a map from original
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
#   *  XYZ work
#   |
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
# - Nested submodules aren't handled yet.  If one of your submodules
#   contains a nested submodule (e.g. clang in llvm/tools/clang where
#   llvm is itself a submodule containing submodule clang), the tool
#   will not find the clang submodule.
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
               subdir, submodule_map_file, update_tags, old_upstream_prefix):
    if not new_upstream_prefix.endswith('/'):
      new_upstream_prefix = new_upstream_prefix + '/'

    if not old_upstream_prefix.endswith('/'):
      old_upstream_prefix = old_upstream_prefix + '/'

    self.new_upstream_prefix     = new_upstream_prefix
    self.old_upstream_prefix     = old_upstream_prefix
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
    self.submodule_revmap        = {}
    self.umbrella_revmap         = {}
    self.update_tags             = update_tags

    if submodule_map_file:
      with open(submodule_map_file) as f:
        self.submodule_map = dict(line.split() for line in f)
    else:
      subprojects = ['clang',
                     'clang-tools-extra',
                     'compiler-rt',
                     'debuginfo-tests',
                     'libclc',
                     'libcxx',
                     'libcxxabi',
                     'libunwind',
                     'lld',
                     'lldb',
                     'llgo',
                     'llvm',
                     'openmp',
                     'parallel-libs',
                     'polly',
                     'pstl']
      self.submodule_map = dict((s, s) for s in subprojects)

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
    new_refs = expand_ref_pattern([self.new_upstream_prefix])

    if not new_refs:
      raise Exception("No refs matched new upstream prefix %s" % self.new_upstream_prefix)

    # Save the set of git hashes for the new monorepo.
    self.new_upstream_hashes = set(subprocess.check_output(['git', 'rev-list'] + new_refs).split('\n')[:-1])

    old_refs = expand_ref_pattern([self.old_upstream_prefix])

    if not old_refs:
      raise Exception("No refs matched old upstream prefix %s" % self.old_upstream_prefix)

    # Save the set of git hashes for the new monorepo.
    self.old_upstream_hashes = set(subprocess.check_output(['git', 'rev-list'] + old_refs).split('\n')[:-1])

  def find_submodules_in_entry(self, githash, tree, path):
    """Figure out which submodules/submodules commit an existing tree references.

    Returns [([submodule pathsegs], commit_hash)], or [] if there are
    no submodule updates to submodules we care about.  commit_hash is
    a reference to the commit pointed to by the submodule gitlink.
    Recurses on subentries and submodules.

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
        else:
          # Recurse on the submodule to see if there are other
          # submodules referenced by it.
          submodule_path = list(path)
          submodule_path.append(name)
          submodule_entry = (submodule_path, e.githash)
          submodules.append(submodule_entry)
          submodules.extend(self.find_submodules_in_entry(e.githash,
                                                          commit.get_tree_entry(),
                                                          submodule_path))

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

    pathsegs = subdir.split('/')

    if tree.mode == '40000':
      entries = tree.get_subentries(self.fm).copy()

      subtree = fast_filter_branch.TreeEntry('40000', sub_entries = entries)

      subtree.write_subentries(self.fm)

      tree = tree.add_path(self.fm, pathsegs, subtree)

      for name, entry in tree.get_subentries(self.fm).items():
        if name is not subdir:
          tree = tree.remove_entry(self.fm, name)

      self.debug('NEWTREE:\n')
      for name, entry in tree.get_subentries(self.fm).items():
        self.debug('%s %s\n' % (name, str(entry)))

    return tree

  def is_ancestor(self, potential_ancestor, potential_descendent):
    ancestor_hash   = self.fm.get_mark(potential_ancestor)
    descendent_hash = self.fm.get_mark(potential_descendent)
    return subprocess.call(["git", "merge-base", "--is-ancestor",
                            ancestor_hash, descendent_hash]) == 0

  def is_same_or_ancestor(self, potential_ancestor, potential_descendent):
    ancestor_hash   = self.fm.get_mark(potential_ancestor)
    descendent_hash = self.fm.get_mark(potential_descendent)
    if ancestor_hash == descendent_hash:
      return True

    return self.is_ancestor(ancestor_hash, descendent_hash)

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

  def remove_submodules(self, tree, submodule_paths, parent_path):
    for pathsegs in submodule_paths:
      entry = tree.get_path(self.fm, pathsegs)
      if entry and entry.mode == '160000':
        self.debug('Removing submodule %s from %s' % ('/'.join(pathsegs),
                                                      parent_path))
        tree = tree.remove_path(self.fm, pathsegs)
    return tree

  def record_mappings(self, newhash, oldhash, updated_submodules):
    """Record the mapping of the original umbrella commit and map
       submodule update hashes to newhash so tags know where to
       point
    """

    # Map the original commit to the new zippped commit.
    self.debug('Mapping umbrella %s to %s' % (oldhash, newhash))
    self.umbrella_revmap[oldhash] = newhash

    # Map the submodule commit to the new zipped commit so we
    # can update tags.
    self.debug('Updated submodules %s' % updated_submodules)
    for sub in updated_submodules:
      self.debug('Mapping submodule %s to %s' % (sub, newhash))
      self.submodule_revmap[sub] = newhash

    return None

  def map_umbrella_commit(self, oldhash):
    newhash = self.umbrella_revmap.get(oldhash)
    if newhash:
      return newhash
    return oldhash

  def map_submodule_commit(self, oldhash):
    newhash = self.submodule_revmap.get(oldhash)
    if newhash:
      return newhash
    return oldhash

  def map_commit(self, oldhash):
    newhash = self.map_submodule_commit(oldhash)
    if newhash != oldhash:
      return newhash
    return self.map_umbrella_commit(oldhash)

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

    # Don't mess with new upstream commits.
    if githash in self.new_upstream_hashes:
      return commit

    # Don't mess with old upstream commits either.  This happens if,
    # for example, downstream uses the llvm repository itself as an
    # umbrella.  We only want to rewrite the downstream commits of
    # such a repository.
    if githash in self.old_upstream_hashes:
      return commit

    self.debug('--- commit %s' % githash)
    self.debug('%s\n' % commit.msg)

    newparents = commit.parents

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
      parent_merge_base = self.umbrella_merge_base.get(op)
      if parent_merge_base:
        umbrella_merge_bases.append([parent_merge_base, None])

    prev_submodules_map = {}

    # Track the old hashes for submodules so we know which
    # submodules this commit updated below.

    # FIXME prev_submodules needs to be mapped by githash and checked
    # from oldparents to handle branches in the umbrella.
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

      if newhash in self.new_upstream_hashes:
        self.debug("Upstream submodule update to %s\n" % newhash)
        commits_to_check.append([newhash, path])

      newcommit = self.fm.get_commit(newhash)
      self.debug('%s\n' % newcommit.msg)

      for parent in newcommit.parents:
        if parent in self.new_upstream_hashes:
          # This submodule has an upstream parent.
          self.debug("Upstream parent %s\n" % parent)
          commits_to_check.append([parent, path])

    umbrella_merge_base_hash = self.get_latest_upstream_commit(githash,
                                                               submodules,
                                                               commits_to_check)

    # Record our choice so children can find it.
    self.umbrella_merge_base[githash] = umbrella_merge_base_hash

    newtree = commit.get_tree_entry()

    # First, remove all submodule updates.  We don't want to rewrite
    # these under subdir.
    newtree = self.remove_submodules(newtree, (x[0] for x in submodules), '.')

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
    submodule_add_parents = []      # Parents due to a "submodule add"
                                    # or update operation
    updated_submodule_hashes = []   # Rewritten commit hash of updated
                                    # submodules
    new_submodules = []             # Submodules we added in this commit
    for pathsegs, oldhash in submodules:
      path='/'.join(pathsegs)

      # Get the hash of the monorepo-rewritten commit corresponding to
      # the submodule update.
      newhash = submodule_hash[path]
      newcommit = self.fm.get_commit(newhash)

      # Map the path in the umbrella history to the path in the
      # monorepo.
      upstream_path = self.submodule_map.get(path)
      if not upstream_path:
        upstream_path = path
      upstream_segs = upstream_path.split('/')

      submodule_tree = newcommit.get_tree_entry().get_path(self.fm,
                                                           upstream_segs)

      if not submodule_tree:
        # This submodule doesn't exist in the monorepo, add the
        # entire contents of the commit's tree.
        submodule_tree = newcommit.get_tree_entry()

      # Remove submodules from this submodule.  Be sure to remove
      # upstrem_segs from the beginning of submodule paths, since that
      # path prefix is what got us to the submodule in the first
      # place.
      subpaths = ('/'.join(x[0]) for x in submodules)
      prefix = path + '/'
      subpaths = (x[x.startswith(prefix) and len(prefix):] for x in subpaths)
      subpaths = (x.split('/') for x in subpaths)
      submodule_tree = self.remove_submodules(submodule_tree, subpaths, path)

      # FIXME: Should we always take the latest version of the
      # upstream tree when a submodule is updated to an upstream
      # commit?  Uncommenting the following will do that.
      #
      # Without the following we will respect the submodule update and
      # set the tree to the referenced commit, even if it means moving
      # the tree for that subdirectory "backwards" from where it is in
      # the upstream linear history relative to other imported
      # submodules.
      #
      # This seems like an ok thing to do since it represents the
      # umbrella history more accurately and it's likely that any
      # submodules added to the umbrella were done in a coordinated
      # fashion and we should respect that.
      #
      # If we're importing a commit from upstream, don't rewrite it,
      # as the umbrella_merge_base_tree has it.
      #if newhash not in self.new_upstream_hashes:
        # Update the tree for the subproject from the commit referenced
        # by the submodule update, overwriting any existing tree for the
        # subproject.

      # Put this under the aboe if to not rewrite submodule trees from
      # upstrem.
      newtree = newtree.add_path(self.fm, upstream_segs, submodule_tree)

      prev_submodule_hash = None
      if path in prev_submodules_map:
        prev_submodule_hash = prev_submodules_map[path]

      if prev_submodule_hash != oldhash:
        if prev_submodule_hash:
          self.debug("Updated %s to %s (new %s)\n" % (path, oldhash, newhash))
          updated_submodule_hashes.append(newhash)

        if not self.no_rewrite_commit_msg:
          if not new_commit_msg:
            new_commit_msg = newcommit.msg
          else:
            new_commit_msg += '\n' + newcommit.msg

      if path not in self.added_submodules:
        self.debug('Add new submodule %s' % path)
        self.added_submodules.add(path)
        updated_submodule_hashes.append(newhash)
        new_submodules.append(newhash)

      # Rewrite parents.
      if path not in self.added_submodules or (prev_submodule_hash != oldhash and prev_submodule_hash):
        # Add parents of the submodule commit if they are not from
        # upstream.  If they are from upstream they will be parented
        # (possibly transitively) through umbrella_merge_base_hash
        # below.
        for parent in newcommit.parents:
          parent_hash = self.fm.get_mark(parent)
          if parent_hash not in self.new_upstream_hashes:
            self.debug('Maybe add parent %s from submodule add or update' % parent_hash)
            submodule_add_parents.append(parent_hash)

    if not oldparents:
      # This is the first commit in the umbrella.
      self.debug('First umbrella commit')
      if len(new_submodules) == 1:
        self.debug('Added a single submodule')
        if len(self.added_submodules) == 1:
          self.debug('Added submodule is only submodule')
          if len(updated_submodule_hashes) != 1:
            raise Exception('Added one new submodule but not exactly one updated hash?')
          # We've added exactly one submodule.
          subhash = updated_submodule_hashes[0]
          if subhash in self.new_upstream_hashes:
            # The submodule commit is from upstream.  Just return the
            # upstream commit as-is.  This avoids duplicated a commit,
            # which would happen since the parent of the new commit
            # would be set to subhash.
            self.debug('Single submodule upstream import, return commit %s' % subhash)
            self.merged_upstream_parents.add(subhash)
            # Tell children of githash that we used a base tree from
            # subhash.
            self.umbrella_merge_base[githash] = subhash
            return self.fm.get_commit(subhash)

    if len(updated_submodule_hashes) == 1:
      # We only updated or added one submodule.  If we're re-writing
      # commit messages, take author, committer and date information
      # from the original commit.  If multiple submodules are updated,
      # take the author, committer and date information from the
      # umbrella commit.
      if not self.no_rewrite_commit_msg:
        subhash = updated_submodule_hashes[0]
        self.debug('Updating author and commiter info from %s' % subhash)
        newcommit = self.fm.get_commit(subhash)

        commit.author         = newcommit.author
        commit.author_date    = newcommit.author_date
        commit.committer      = newcommit.committer
        commit.committer_date = newcommit.committer_date

    upstream_parents = []  # Parents due to merges from upstream

    # Add umbrella_merge_base as a parent if it's a descendent of all
    # previously merged upstream commits.
    if umbrella_merge_base_hash in self.new_upstream_hashes:
      if umbrella_merge_base_hash not in self.merged_upstream_parents:
        if self.list_is_ancestor(self.merged_upstream_parents, umbrella_merge_base_hash):
          # The new merge-base is newer than all previously-merged
          # upstream parents, so add an edge to it.
          self.debug('Maybe add upstream merge parent %s' % umbrella_merge_base_hash)
          upstream_parents.append(umbrella_merge_base_hash)
          self.merged_upstream_parents.add(umbrella_merge_base_hash)

    newtree.write_subentries(fm)
    commit.treehash = newtree.githash

    for name, e in newtree.get_subentries(fm).iteritems():
      self.debug('NEWTREE: %s %s' % (name, str(e)))

    added_parents = submodule_add_parents
    added_parents.extend(upstream_parents)

    for addparent in added_parents:
      doadd = True
      for newparent in newparents:
        if self.is_same_or_ancestor(addparent, newparent):
          self.debug('Filtering potential new parent %s which is ancestor of %s' % (addparent, newparent))
          doadd = False
          break
      if doadd:
        commit.parents.append(addparent)

    commit.msg = new_commit_msg

    return (commit,
            lambda newhash,updated_submodules = updated_submodule_hashes, oldhash = githash:
            self.record_mappings(newhash, oldhash, updated_submodules))

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
    # Note that thil will not update any tags in the histories pointed
    # to by submodulees, since we don't ever rewrite those commits.
    # The call to update_refs below updates those tags.
    fast_filter_branch.do_filter(commit_filter=self.zip_filter,
                                 filter_manager=self.fm,
                                 revmap_filename=self.revmap_out_file,
                                 reflist=expand_ref_pattern(self.reflist))

    if self.update_tags:
      print "Updating tags..."
      fast_filter_branch.update_refs(self.fm, ['refs/tags'],
                                     self.submodule_revmap, None, None, None)

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
  parser.add_argument("--old-repo-prefix", metavar="REFPATTERN", default="refs/remotes/old",
                      help="The prefix for all the refs of the old repository/repositories (default: %(default)s).")
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
  parser.add_argument("--submodule-map", metavar="FILE",
                      help="File containing a map from submodule path to monorepo path")
  parser.add_argument("--update-tags", action="store_true",
                      help="Update tags after filtering")
  args = parser.parse_args()
  Zipper(args.new_repo_prefix, args.revmap_in, args.revmap_out, args.reflist,
         args.debug, args.abort_bad_submodule, args.no_rewrite_commit_msg,
         args.subdir, args.submodule_map, args.update_tags,
         args.old_repo_prefix).run()
