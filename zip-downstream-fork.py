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
# On the rewriting of trees and parents:
#
# The tool takes care to preserve the proper history for upstream
# monorepo bits that do not participate in the submodule process.  For
# example, say the umbrella history looks like this:
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
#   *   Do commit QUUZ in clang
#   |
#   |  *   (HEAD -> monorepo-clang/local) Do commit FOO in clang
#   |  |
#   | /
#   |/
#   *   Do commit QUUX in compiler-rt
#   |
#   *   (tag: llvmorg-10.0.0) Do commit GARPLY in clang
#   |
#
# The commits from compiler-rt come from upstream (no local work
# exists for compiler-rt) but commits BAR and BAZ exist in local
# histories for llvm and clang which were rewritten against the
# upstream monorepo (i.e. they are in branches off monorepo/master or
# some other point).
#
# The tool rewrites parents to indicate which tree was used for an
# inlined submodule commit:
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
#   *   /   Do commit QUUZ in clang
#   |  /
#   | /
#   |/
#   *   Do commit QUUX in compiler-rt
#   |
#   *   (tag: llvmorg-10.0.0) Do commit GARPLY in clang
#   |
#
# The edge from compiler-rt/QUUX to zip/master appears redundant (it
# was supposedly merged along with compiler-rt/BAZ).  However,
# according to the submodule history, clang/FOO should be paired with
# llvm/BAR.  clang/FOO is based on clang/GARPLY and any files not
# touched by clang/FOO will reflect their state at clang/GARPLY, not
# their state at clang/QUUZ.  Therefore, the tool keeps the edge from
# compiler-rt/QUUX as a visual reminder of the state of the tree.  The
# script favors preserving submodule updates and their trees as they
# appeared in the umbrella history rather than trying to merge local
# changes into the latest version of a tree.
#
# The tool takes care to correct write trees for subprojects not
# participating in the umbrella history.  Given the above graph, a
# naive tree rewriting would result in compiler-rt being written
# incorrectly, resulting in compiler-rt/QUUX at zip/master rather than
# the proper compiler-rt/BAZ.  This is because monorepo-clang/FOO
# incorpates the tree from compiler-rt/QUUX
#
# The script attempts to get this right by tracking the most recent
# upstream commit that has been merged from the monorepo along each
# zipped branch.  If a submodule update brings in an older tree from
# the monorepo that doesn't participate in submodule history, that
# tree is discarded in favor of the more recent tree.  This means that
# the script assumes there is a total topological ordering among
# upstream commits brought in via submodule updates.  For example, the
# script will abort if trying to create a history like this:
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
# If there are downstream commits that are not inlined into the
# zippped history (because submodule updates did not directly bring
# them in), their parents will be rewritten to point to inlined
# commits.  For example, given this umbrella history with llvm/GRAULT
# not updated in the umbrella:
#
#   *   (HEAD -> umbrella/master) Update submodule clang to FOO
#   |
#   *   Update submodule llvm to BAR
#   |
#   |  *   (HEAD -> llvm/local) Do commit BAR in llvm
#   |  |
#   |  *   Do commit GRAULT in llvm
#   |  |
#   |  *   Do commit WALDO in llvm
#   |  |
#   |  |     *   (HEAD -> clang/local) Do commit FOO in clang
#   |  |     |
#   *  |     |        Downstream umbrella work
#   |  |     |
#     llvm  clang
#
# With the monorepo history from above, the zipped history will look
# like this:
#
#   *   (HEAD -> monorepo/master) Do commit XYZZY in lld
#   |
#   |  *   (HEAD -> zip/master) Do commit FOO in clang
#   |  |\
#   |  * \   Do commit BAR in llvm
#   |  |  |
#   |  *  |   Do commit GRAULT in llvm
#   |  |  |
#   |  *  |   Do commit WALDO in llvm
#   | /   |
#   |/    |
#   *     |   Do commit BAZ in compiler-rt
#   |    /
#   *   /   Do commit QUUZ in clang
#   |  /
#   | /
#   |/
#   *   Do commit QUUX in compiler-rt
#   |
#   *   (tag: llvmorg-10.0.0) Do commit GARPLY in clang
#   |
#
# Parent rewriting happens in two stages.  Stage1 updates the parents
# of commits that were inlined into the zippped history.  Stage2
# updates the parents of commits that were not inlined.  The reason
# this must happen in two stages is that in stage1 we are traversing
# the old umbrella history.  The only other commits we see are those
# from the gitlinks pointing to submodule commits we want to inline
# and we use the trees of those commits to get the right blobs into
# the rewritten umbrella commits, effectively inlining the gitlinks.
# The new umbrella commits are the only commits where we can update
# parents in stage1.  In stage2, the new umbrella (zipped) commits now
# have parents pointing to submodule commits that were not inlined
# (thanks to the parent rewriting in stage1) and thus we will see
# those commits in stage2 and can rewrite their parents.
#
# On the rewriting of tags
#
# With the --update-tags option, the script will rewrite any tags
# pointing to inlined submodule commits to point at the new inlined
# commit.  No attempt is made to distinguish upstream tags from local
# tags.  Therefore, rewriting could be surprising, as in this example:
#
#   *   (HEAD -> umbrella/master) Get upstream clang/GARPLY
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
#   *   (HEAD -> monorepo/master) Do commit XYZZY in lld
#   |
#   |  *   (HEAD -> zip/master) (tag: llvmorg-10.0.0) Getupstream clang/GARPLY
#   |  |\
#   |  * \   Do commit BAR in llvm
#   | /   |
#   |/    |
#   *     |   Do commit BAZ in compiler-rt
#   |     |
#   *     |  Do commit QUUZ in clang
#   |    /
#   |   /
#   |  /
#   * /   Do commit QUUX in compiler-rt
#   |/
#   *   Do commit GARPLY in clang (previously tagged llvmorg-10.0.0)
#   |
#
# The umbrella pulled in a commit directly from upstream which
# happened to have a tag associated with it and so when it was inlined
# into the zipped history with --update-tags, the tag was rewritten to
# point to the inlined commit.  This is almost certainly not what is
# wanted, which is why rewriting tags is an optional feature.
# However, this is probably an uncommon occurrence and it is generally
# safe and correct to use --update-tags.  If upstream tags happen to
# be rewritten it is always possible to move the tag back to its
# correct location.
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
# - Submodule removal is not handled at all.  A third-party subproject
#   will continue to exist though no updates to it will be made.  This
#   could by added by judicial use of fast_filter_branch.py's
#   TreeEntry.remove_entry.  For projects managed by upstream (clang,
#   llvm, etc.), if a commit doesn't include a submodule (because it
#   was removed), the subproject tree is taken from the upstream
#   monorepo tree just as it is for upstream projects not
#   participating in the umbrella history.
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

    # Options
    self.new_upstream_prefix   = new_upstream_prefix
    self.old_upstream_prefix   = old_upstream_prefix
    self.revmap_in_file        = revmap_in_file
    self.revmap_out_file       = revmap_out_file
    self.reflist               = reflist
    self.dbg                   = debug
    self.new_upstream_hashes   = set()
    self.abort_bad_submodule   = abort_bad_submodule
    self.no_rewrite_commit_msg = no_rewrite_commit_msg
    self.subdir                = subdir
    self.update_tags           = update_tags

    # Filter state

    # Latest merged upstream parents for each submodule, indexed by
    # old umbrella parent.
    self.merged_upstream_parents         = {}

    # Latest merged downstream parents for each submodule, indexed by
    # old umbrella parent.
    self.merged_downstream_parents       = {}


    # Map from old downstream commit hash to new downstream commit
    # hash.
    self.revap                           = {}


    # Most-recently-merged commit of each submodule, indexed by old
    # umbrella parents.
    self.prev_submodules                 = {}

    # Map from old umbrella commit to the upstream commit used for the
    # base tree of the corresponding new commit.
    self.base_tree_map                   = {}

    # Map from old umbrella commit hash or original submodule commit
    # to map from submodule path to the original submodule commit
    # tree.  Used to determine which submodules were inlined into each
    # umbrella commit.
    self.submodule_tree_map              = {}

    # Set of hashes of monorepo commits inlined into umbrella commits.
    self.inlined_submodule_commits       = set()

    # Map from original downstream or umbrella commit to its final
    # commit in the zippped history.  Used for rewriting tags.
    self.tag_revmap                      = {}

    # Map from monorepo-rewritten submodule commit hash to new
    # umbrella commit hash/mark where it was inlined.  Used for
    # updating parents of non-inlined submodule commits.
    self.inlined_submodule_revmap        = {}

    # Map from new umbrella commit/hash to new submodule commit hashes
    # inlined into it.
    self.stage1_submodule_reverse_revmap = {}

    # Map from old umbrella commit hash to new umbrella commit
    # hash/mark after stage1.
    self.stage1_umbrella_revmap          = {}

    # Map from old umbrella commit hash to new umbrella commit
    # hash/mark after stage2.
    self.stage2_umbrella_revmap          = {}

    # Map from new umbrella commit hash/mark to old umbrella commit
    # hash.
    self.stage1_umbrella_old_revmap      = {}

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

  def is_mark(self, mark):
    if mark.startswith(':'):
      return True
    return False

  def is_ancestor(self, potential_ancestor, potential_descendent):
    if self.is_mark(potential_ancestor):
      raise Exception('Cannot check ancestry of mark %s' % potential_ancestor)
    if self.is_mark(potential_descendent):
      raise Exception('Cannot check ancestry of mark %s' % potential_descendent)

    return subprocess.call(["git", "merge-base", "--is-ancestor",
                            potential_ancestor, potential_descendent]) == 0

  def is_same_or_ancestor(self, potential_ancestor, potential_descendent):
    if self.is_mark(potential_ancestor):
      raise Exception('Cannot check ancestry of mark %s' % potential_ancestor)
    if self.is_mark(potential_descendent):
      raise Exception('Cannot check ancestry of mark %s' % potential_descendent)

    if potential_ancestor == potential_descendent:
      return True

    return self.is_ancestor(potential_ancestor, potential_descendent)

  def list_is_ancestor(self, potential_ancestors, potential_descendent):
    for potential_ancestor in potential_ancestors:
      if not self.is_ancestor(potential_ancestor, potential_descendent):
        return False
    return True

  def is_same_or_ancestor_of_any(self, potential_ancestor, potential_descendents):
    for potential_descendent in potential_descendents:
      if self.is_same_or_ancestor(potential_ancestor, potential_descendent):
        return potential_descendent

    return None

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
        # upstream branches.  We don't handle this yet as it would
        # require merging the trees.
        warnstr = "Commit %s %s: no order between (%s %s)\n\n" % (githash, path,
                                                                  result, candidate)
        for pathsegs, oldhash in submodules:
          errpath = '/'.join(pathsegs)
          errstr += "%s %s\n" % (errpath, oldhash)

        print('WARNING: %s' % warnstr)
        return None

    return result

  def remove_submodules(self, tree, submodule_paths, parent_path):
    for pathsegs in submodule_paths:
      entry = tree.get_path(self.fm, pathsegs)
      if entry and entry.mode == '160000':
        self.debug('Removing submodule %s from %s' % ('/'.join(pathsegs),
                                                      parent_path))
        tree = tree.remove_path(self.fm, pathsegs)
    return tree

  def record_stage1_mappings(self, newhash, oldhash, updated_submodules,
                             inlined_submodules):
    """Record the mapping of the original umbrella commit and map
       submodule update hashes to newhash so tags know where to
       point.
    """

    # Map the original commit to the new zippped commit.
    mapped_newhash = self.fm.get_mark(newhash)
    self.debug('Mapping umbrella %s to %s' % (oldhash, mapped_newhash))
    self.stage1_umbrella_revmap[oldhash] = mapped_newhash

    self.stage1_umbrella_old_revmap[mapped_newhash] = oldhash

    # Map the submodule commit to the new zipped commit so we can
    # update tags.  These mappings will be used in stage2 to
    # eventually map the original submodule commit to its final commit
    # in zipped history.
    self.debug('Updated submodules %s' % updated_submodules)
    self.debug('Inlined submodules %s' % inlined_submodules)
    for pathsegs, oldshash, newshash in inlined_submodules:
      path = '/'.join(pathsegs)
      self.debug('Mapping inlined submodule %s %s to %s' %
                 (path, newshash, mapped_newhash))
      self.inlined_submodule_revmap[newshash] = mapped_newhash

      reverse_submodule_mapping = self.stage1_submodule_reverse_revmap.get(mapped_newhash)
      if reverse_submodule_mapping:
        reverse_submodule_mapping.add(newshash)
      else:
        reverse_submodule_mapping = set()
        reverse_submodule_mapping.add(newshash)
      self.stage1_submodule_reverse_revmap[mapped_newhash] = reverse_submodule_mapping

    return None

  def record_stage2_mappings(self, newziphash, oldziphash):
    """Record the mapping of the original umbrella commit and map
       submodule update hashes to newhash so tags know where to
       point.
    """

    mapped_oldziphash = self.fm.get_mark(oldziphash)
    mapped_newziphash = self.fm.get_mark(newziphash)

    # If this is an original umbrella commit, map it to the stage2
    # zippped commit.
    orig_umbrella_hash = self.stage1_umbrella_old_revmap.get(mapped_oldziphash)
    if orig_umbrella_hash:
      self.debug('Mapping original umbrella %s to %s' %
                 (orig_umbrella_hash, mapped_newziphash))
      self.stage2_umbrella_revmap[orig_umbrella_hash] = newziphash

      # Map submodule commits inlined into this umbrella from their
      # original downstream commits to the new zippped commit.
      reverse_submodule_mapping = self.stage1_submodule_reverse_revmap.get(mapped_oldziphash)
      if reverse_submodule_mapping:
        for newshash in reverse_submodule_mapping:
          self.debug('Mapping inlined submodule downstream commit %s to %s' %
                     (newshash, mapped_newziphash))
          self.inlined_submodule_revmap[newshash] = newziphash
          self.tag_revmap[newshash] = newziphash
    else:
      # Otherwise, this is a downstream commit that may or may not be
      # linked into the final zipped history.  If it was inlined, it
      # will not be linked in (children will have their parent
      # rewritten to the zippped commit where it was inlined) and its
      # mapping for the purpose of tag rewriting was done above when
      # its corresponding umbrella commit was processed.  If it was
      # not inlined, record the mapping from its original downstream
      # commit to its new downstream commit (they may differ due to
      # parent rewriting).
      if not oldziphash in self.inlined_submodule_commits:
        self.debug('Mapping non-inlined downstream %s to %s' %
                   (mapped_oldziphash, mapped_newziphash))
        self.stage2_umbrella_revmap[mapped_oldziphash] = newziphash
        # Map this non-inlined downstream commit to newziphash to we can
        # update tags.
        self.tag_revmap[mapped_oldziphash] = newziphash

    return None

  def get_base_tree_commit_hash(self, fm, githash, commit, oldparents, submodules):
    """Determine the base tree for the rewritten umbrella commit"""
    # The content of the commit should be the combination of the
    # content from the submodules and elements from the monorepo tree
    # not updated by submodules.  The tricky part is figuring out
    # which monorepo tree that should be.

    # Check to see if this commit is actually a monorepo-rewritten
    # commit.  If it is, use it as the base tree.  This happens if an
    # upstream project has submodules added to it.
    mapped_githash = self.revmap.get(githash)
    if mapped_githash:
      self.debug('Using mapped umbrella commit %s as base tree' % mapped_githash)
      return mapped_githash

    # Check all of the upstream ancestors and see which is the
    # earliest.
    commits_to_check = []

    # Add the merge base from the umbrella's parents to the candidate
    # list.  Also check for upstream parents which are also
    # candidates.
    for op in oldparents:
      self.debug('Checking umbrella parent %s for merge base' % op)
      parent_merge_base = self.base_tree_map.get(op)
      if parent_merge_base:
        self.debug('Adding parent merge base %s to merge base set' % parent_merge_base)
        commits_to_check.append([parent_merge_base, '.'])
      mapped_op = self.revmap.get(op)
      if mapped_op:
        # The umbrella commit itself has a monorepo-rewritten parent.
        # This can happen if submodules were added to an upstream
        # project.
        self.debug('Adding monorepo parent %s to merge base set' % mapped_op)
        commits_to_check.append([mapped_op, '.'])

    for pathsegs, oldhash in submodules:
      path='/'.join(pathsegs)
      self.debug('Found submodule (%s, %s)' % (path, oldhash))

      # Get the hash of the monorepo-rewritten commit corresponding to
      # the submodule update.
      newhash = self.revmap.get(oldhash, oldhash)
      self.debug('New hash: %s' % newhash)

      if newhash in self.new_upstream_hashes:
        self.debug("Upstream submodule update to %s\n" % newhash)
        commits_to_check.append([newhash, path])

      newcommit = self.fm.get_commit(newhash)
      self.debug('%s\n' % newcommit.msg)

      for parent in newcommit.parents:
        if parent in self.new_upstream_hashes:
          # This submodule has an upstream parent.  It is a candidate
          # for the base tree.
          self.debug("Upstream parent %s\n" % parent)
          commits_to_check.append([parent, path])

    result = self.get_latest_upstream_commit(githash, submodules,
                                             commits_to_check)

    if not result:
      raise Exception('Umbrella incorprated submodules from multiple monorepo branches')

    self.debug('Using commit %s as base tree' % result)

    return result

  def submodule_was_added_or_updated(self, oldparents, submodule_path,
                                     submodule_oldhash):
    """Return whether submodule_oldhash represents an addition of a new
       submodule or an update of an existing submodule."""

    # If submodule_oldhash matches any submodule along oldparents,
    # this is not a submodule add or update.
    for op in oldparents:
      prev_submodules_map = self.prev_submodules.get(op)
      if prev_submodules_map:
        prev_submodule_hash = prev_submodules_map.get(submodule_path)
        if prev_submodule_hash and prev_submodule_hash == submodule_oldhash:
          return False

    return True

  def get_updated_or_added_submodules(self, githash, commit, oldparents,
                                      submodules):
    """Return a list of (submodule, oldhash, newhash) for each submodule
       that was newly added or updated in this commit."""
    prev_submodules_map = {}

    for op in oldparents:
      self.prev_submodules[op] = set()

    updated_submodules = []
    for pathsegs, oldhash in submodules:
      path='/'.join(pathsegs)
      if self.submodule_was_added_or_updated(oldparents, path, oldhash):

        # Get the hash of the monorepo-rewritten commit corresponding to
        # the submodule update.
        newhash = self.revmap.get(oldhash, oldhash)
        updated_submodules.append((pathsegs, oldhash, newhash))

    # Record the submodule state for this commit.
    self.prev_submodules[githash] = submodules

    self.debug('Updated or added submodules: %s' % updated_submodules)
    return updated_submodules

  def submodule_tree_in_old_parents(self, path, submodule_tree, parents):
    """Return whether submodule_tree appears in any of parents."""
    for p in parents:
      parent_commit = self.fm.get_commit(p)
      parent_tree = parent_commit.get_tree_entry()
      if not parent_tree:
        raise Exception('Could not find submodule %s in old parent %s' % (path, p))

      if parent_tree == submodule_tree:
        self.debug('submodule tree %s' % str(submodule_tree))
        self.debug('parent tree    %s' % str(parent_tree))
        self.debug('%s tree in old parent %s' % (path, p))
        return True

    return False

  def submodule_tree_in_umbrella_parents(self, pathsegs, submodule_tree,
                                         parents):
    """Return whether submodule_tree was written into any of parents."""
    path = '/'.join(pathsegs)
    for p in parents:
      parent_submodule_tree_map = self.submodule_tree_map.get(p)
      if parent_submodule_tree_map:
        parent_submodule_tree = parent_submodule_tree_map.get(path)
        if parent_submodule_tree and parent_submodule_tree == submodule_tree:
          self.debug('submodule tree %s' % str(submodule_tree))
          self.debug('parent tree    %s' % str(parent_submodule_tree))
          self.debug('%s tree in umbrella parent %s' % (path, p))
          return True

    return False

  def get_inlined_submodules(self, githash, commit, oldparents,
                             updated_submodules):
    """Return a list of (submodule, oldhash, newhash) for each submodule
       that will be inlined into this commit.  This differs from
       updated or added submodules in that updated or added means the
       tree for the submodule changes from the previous zipped commit
       while inlined means the tree differs from all parents,
       including any downstream parents."""

    inlined_submodules = []
    for pathsegs, oldhash, newhash in updated_submodules:
      path='/'.join(pathsegs)
      newcommit = self.fm.get_commit(newhash)

      submodule_tree = newcommit.get_tree_entry()
      if not submodule_tree:
        raise Exception('Could not find submodule %s in submodule commit %s' %
                        (path, newhash))

      # Check previously-merged upstream and downstream parents.  If
      # their submodule tree is the same as the updated submodule,
      # then the submodule was NOT inlined in this commit.
      merged_parents = []
      for op in oldparents:
        upstream_map = self.merged_upstream_parents.get(op)
        if upstream_map:
          upstream_parents = upstream_map.get(path)
          if upstream_parents:
            merged_parents.extend(upstream_parents)

        downstream_map = self.merged_downstream_parents.get(op)
        if downstream_map:
          downstream_parents = downstream_map.get(path)
          if downstream_parents:
            merged_parents.extend(downstream_parents)

      if not self.submodule_tree_in_old_parents(path, submodule_tree, merged_parents):
        if not self.submodule_tree_in_umbrella_parents(pathsegs, submodule_tree, oldparents):
          self.debug('Inlined %s %s' % (path, newhash))
          self.inlined_submodule_commits.add(newhash)
          inlined_submodules.append((pathsegs, oldhash, newhash))

    self.debug('Inlined submodules: %s' % inlined_submodules)
    return inlined_submodules

  def update_merged_parents(self, githash, submodules):
    """Record the upstream and downstream parents of updated
       submodules."""
    self.merged_upstream_parents[githash]   = {}
    self.merged_downstream_parents[githash] = {}

    for pathsegs, oldhash, in submodules:
      path='/'.join(pathsegs)

      # Get the hash of the monorepo-rewritten commit corresponding to
      # the submodule update.
      newhash = self.revmap.get(oldhash, oldhash)

      # Get the monorepo-rewritten submodule commit.
      newcommit = self.fm.get_commit(newhash)

      upstream_parents = []
      downstream_parents = []
      for p in newcommit.parents:
        if p in self.new_upstream_hashes:
          upstream_parents.append(p)
          continue
        downstream_parents.append(p)

      # Also include the submodule commit itself.
      if newhash in self.new_upstream_hashes:
        upstream_parents.append(newhash)
      else:
        downstream_parents.append(newhash)

      self.merged_upstream_parents[githash][path]   = upstream_parents
      self.merged_downstream_parents[githash][path] = downstream_parents

  def determine_parents(self, fm, githash, commit, oldparents, submodules,
                        updated_submodules):
    # Rewrite existing new parents.  If the umbrella is actually an
    # upstream project that's had submodules added to it, then this
    # commit and its parents are actually split commits, not monorepo
    # commits.  Otherwise commit is a rewritten umbrella commit and
    # its parents were already rewritten.
    parents = []
    rewritten_or_upstream_parents = []
    for np in commit.parents:
      # Sometimes fast_filter_branch sets a parent to a mark even if
      # the parent is an upstream monorepo commit.  We want the real
      # commit hash if it's available.
      np_hash = self.fm.get_mark(np)
      mapped_np = self.revmap.get(np_hash)
      if mapped_np:
        parents.append(mapped_np)
        rewritten_or_upstream_parents.append(mapped_np)
      else:
        parents.append(np)
      if np_hash in self.new_upstream_hashes:
        rewritten_or_upstream_parents.append(np_hash)

    # Check submodules that were added or updated.  If their commits
    # have parents not already included, add them.
    submodule_upstream_parent_candidates = []
    for pathsegs, oldhash, newhash in updated_submodules:
      path='/'.join(pathsegs)

      newcommit = self.fm.get_commit(newhash)

      # Gather previously-merged upstream and downstream parents.
      merged_upstream_parents   = []
      merged_downstream_parents = []
      for op in oldparents:
        upstream_map = self.merged_upstream_parents.get(op)
        if upstream_map:
          upstream_parents = upstream_map.get(path)
          if upstream_parents:
            merged_upstream_parents.extend(upstream_parents)

        downstream_map = self.merged_downstream_parents.get(op)
        if downstream_map:
          downstream_parents = downstream_map.get(path)
          if downstream_parents:
            merged_downstream_parents.extend(downstream_parents)

      for p in newcommit.parents:
        if p in self.new_upstream_hashes:
          # This is a rewritten upstream commit.
          maybe_descendent = self.is_same_or_ancestor_of_any(p, merged_upstream_parents)
          if maybe_descendent:
            self.debug('Filtering submodule %s upstream parent %s which is ancestor of %s' %
                       (path, p, maybe_descendent))
            continue

          self.debug('Add submodule %s upstream parent %s' % (path, p))
          parents.append(p)

          continue

        # This submodule parent is a monorepo-rewritten downstream
        # commit.
        maybe_descendent = self.is_same_or_ancestor_of_any(p, merged_downstream_parents)
        if maybe_descendent:
          self.debug('Filtering submodule %s downstream parent %s which is ancestor of %s' %
                     (path, p, maybe_descendent))
          # Remember this as it might be a descendent of an upstream
          # parent candidate.
          continue

        self.debug('Add downstream parent %s from submodule %s add or update' %
                   (p, path))
        parents.append(p)

    self.debug('New parents: %s' % parents)
    return parents

  def get_commit_message(self, githash, commit, oldparents, submodules,
                         inlined_submodules):
    if self.no_rewrite_commit_msg:
      return commit.msg

    if len(inlined_submodules) == 1:
      # We only inlined one submodule.  This commit will be inlined so
      # use the submodule commit's message.
      pathsegs, oldhash, newhash = inlined_submodules[0]
      newcommit = self.fm.get_commit(newhash)
      return newcommit.msg

    # We inlined zero or more than one submodule.  Include the
    # original umbrella commit to avoid confusion with log --oneline
    # listings, which would show two commits with the same subject
    # otherwise.
    newmsg = commit.msg
    for pathsegs, oldhash, newhash in inlined_submodules:
      newcommit = self.fm.get_commit(newhash)
      newmsg = newmsg + '\n' + newcommit.msg

    self.debug('Updating commit message to:\n %s\n' % newmsg)
    return newmsg

  def get_author_info(self, commit, inlined_submodules):
    if len(inlined_submodules) == 1:
      # We only updated or added one submodule.  If we're re-writing
      # commit messages, take author, committer and date information
      # from the original commit.  If multiple submodules are inlined,
      # take the author, committer and date information from the
      # umbrella commit.
      if not self.no_rewrite_commit_msg:
        pathsegs, oldhash, newhash = inlined_submodules[0]
        self.debug('Updating author and commiter info from %s' % newhash)
        newcommit = self.fm.get_commit(newhash)

        commit.author         = newcommit.author
        commit.author_date    = newcommit.author_date
        commit.committer      = newcommit.committer
        commit.committer_date = newcommit.committer_date
    return commit

  def rewrite_tree(self, githash, commit, base_tree, submodules):
    # Remove submodules from the base tree.
    self.debug('Removing submomdules from the base tree')
    base_tree = self.remove_submodules(base_tree, (x[0] for x in submodules), '.')

    umbrella_is_rewritten_downstream_commit = False
    if githash in self.revmap:
      umbrella_is_rewritten_downstream_commit = True

    # If the umbrella commit is actually a rewritten downstream
    # commit, then a subproject had submodules added to it.  If so,
    # the base tree is from that commit and already had submodules
    # removed.

    if not umbrella_is_rewritten_downstream_commit:
      # This is a "proper" umbrella commit composed of submodules
      # along with possibly other tree entries not related to
      # submodules.  We need to put the non-submodule pieces under
      # subdir.

      # Remove submodules from the commit tree.
      self.debug('Removing submomdules from the proper umbrella commit tree')
      commit_tree = commit.get_tree_entry()
      commit_tree = self.remove_submodules(commit_tree, (x[0] for x in submodules), '.')

      # Rewrite the remaining bits under subdir in the base tree.
      self.debug('Rewrite non-submodule entries')
      pathsegs = self.subdir.split('/')
      base_tree = base_tree.add_path(self.fm, pathsegs, commit_tree)

    # Add the submodule trees to the commit, overwriting whatever
    # might already be there.  We prefer the tree to represent the
    # submodule state of the original umbrella commit.
    for pathsegs, oldhash in submodules:
      path='/'.join(pathsegs)

      # Get the hash of the monorepo-rewritten commit corresponding to
      # the submodule update.
      newhash = self.revmap.get(oldhash, oldhash)
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

      # Record this tree for this submodule written into umbrella
      # commit githash.  Do this before removing submodules so we see
      # changes in the tree on submodule updates when determining
      # which submodules were inlined to each umbrella commit.  A
      # submodule was "inlined" even if the only thing that changed in
      # it was updates of submodules under it.
      tree_map = self.submodule_tree_map.get(githash)
      if not tree_map:
        tree_map = {}

      tree_map[path] = submodule_tree
      self.submodule_tree_map[githash] = tree_map

      # Remove submodules from this submodule.  Be sure to remove
      # upstrem_segs from the beginning of submodule paths, since that
      # path prefix is what got us to the submodule in the first
      # place.
      self.debug('Removing submomdules from submodule %s' % path)
      subpaths = ('/'.join(x[0]) for x in submodules)
      prefix = path + '/'
      subpaths = (x[x.startswith(prefix) and len(prefix):] for x in subpaths)
      subpaths = (x.split('/') for x in subpaths)
      submodule_tree = self.remove_submodules(submodule_tree, subpaths, path)

      self.debug('Writing submodule %s %s to base tree' % (path, newhash))
      base_tree = base_tree.add_path(self.fm, upstream_segs, submodule_tree)

    base_tree.write_subentries(self.fm)
    commit.treehash = base_tree.githash

    for name, e in base_tree.get_subentries(self.fm).iteritems():
      self.debug('NEWTREE: %s %s' % (name, str(e)))

    return commit

  def submodule_parent_filter(self, fm, ziphash, commit, zipparents):
    """Rewrite any parents of non-inlined submodule commits to point to
    inlined submodule commits."""

    # Don't mess with monorepo upstream commits.
    if ziphash in self.new_upstream_hashes:
      return commit

    # Don't mess with old upstream commits either.  This happens if,
    # for example, downstream uses the llvm repository itself as an
    # umbrella.  We only want to rewrite the downstream commits of
    # such a repository.
    if ziphash in self.old_upstream_hashes:
      return commit

    self.debug('--- zip commit %s' % ziphash)
    self.debug('%s\n' % commit.msg)

    # If this is an inlined submodule commit, just return it, no
    # parent rewriting need be done.
    if self.fm.get_mark(ziphash) in self.stage1_umbrella_old_revmap:
      self.debug('Inlined, not updating parents')
      return (commit,
              lambda newhash, oldhash = ziphash:
              self.record_stage2_mappings(newhash, oldhash))

    newparents = []
    for zp in zipparents:
      # If commit is a submodule commit that was not inlined, zp could
      # be a monorepo-rewritten commit that was inlined into zipped
      # history.  If so, inlined_submodule_revmap tells us which
      # zipped commit it was inlined into.
      newparent = self.inlined_submodule_revmap.get(zp, zp)
      self.debug('Found new parent %s for zip parent %s' % (newparent, zp))
      newparents.append(newparent)

    if newparents != zipparents:
      mapped_parents = [self.fm.get_mark(p) for p in newparents]
      self.debug('Updating parents of non-inlined %s from %s to %s' %
                 (ziphash, zipparents, mapped_parents))
      commit.parents = newparents

    return (commit,
            lambda newhash, oldhash = ziphash:
            self.record_stage2_mappings(newhash, oldhash))


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

    # Don't mess with monorepo upstream commits.
    if githash in self.new_upstream_hashes:
      return commit

    # Don't mess with old upstream commits either.  This happens if,
    # for example, downstream uses the llvm repository itself as an
    # umbrella.  We only want to rewrite the downstream commits of
    # such a repository.
    if githash in self.old_upstream_hashes:
      return commit

    submodules = self.find_submodules(commit, githash)

    # If there are no submodules and this is a monorepo-rewritten
    # commit, just return the new commit.  This happens if an upstream
    # project is used as an umbrella (e.g. llvm contains a submodule
    # in llvm/tools/clang) and local commits are made to it before
    # adding submodules.
    if not submodules:
      newhash = self.revmap.get(githash)
      if newhash:
        newcommit = self.fm.get_commit(newhash)

        # Tell children of githash that we used a base tree from
        # newhash.
        self.base_tree_map[githash] = newhash
        self.update_merged_parents(githash, submodules)

        return newcommit

    self.debug('--- commit %s' % githash)
    self.debug('%s\n' % commit.msg)

    newparents = commit.parents

    updated_submodules = self.get_updated_or_added_submodules(githash, commit,
                                                              oldparents,
                                                              submodules)

    if not oldparents:
      # This is the first commit in the umbrella.
      self.debug('First umbrella commit')
      if len(updated_submodules) == 1:
        pathsegs, oldhash, newhash = updated_submodules[0]
        if newhash in self.new_upstream_hashes:
          # The submodule commit is from upstream.  Just return the
          # upstream commit as-is.  This avoids duplicated a commit,
          # which would happen since the parent of the new commit
          # would be set to subhash.
          self.debug('Single submodule upstream import, return commit %s' % newhash)
          # Tell children of githash that we used a base tree from
          # newhash.
          self.base_tree_map[githash] = newhash
          self.update_merged_parents(githash, submodules)
          return self.fm.get_commit(newhash)

    # Determine the base tree.
    base_tree_commit_hash = self.get_base_tree_commit_hash(fm, githash, commit,
                                                           oldparents, submodules)

    # Record our choice so children can find it.
    self.base_tree_map[githash] = base_tree_commit_hash

    base_tree_commit = fm.get_commit(base_tree_commit_hash)
    base_tree = base_tree_commit.get_tree_entry()

    commit = self.rewrite_tree(githash, commit, base_tree, submodules)

    # Rewrite parents.
    commit.parents = self.determine_parents(fm, githash, commit, oldparents,
                                            submodules, updated_submodules)

    inlined_submodules = self.get_inlined_submodules(githash, commit,
                                                     oldparents,
                                                     updated_submodules)

    self.update_merged_parents(githash, submodules)

    commit.msg = self.get_commit_message(githash, commit, oldparents,
                                         submodules, inlined_submodules)

    commit = self.get_author_info(commit, inlined_submodules)

    return (commit,
            lambda newhash, changed_submodules = updated_submodules,
                   inl_submodules = inlined_submodules, oldhash = githash:
            self.record_stage1_mappings(newhash, oldhash, changed_submodules,
                                        inl_submodules))

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

    # stage1 - Replace submodule update trees with trees from their
    # corresponding subproject commits.
    print "Zipping commits..."
    # Note that thil will not update any tags in the histories pointed
    # to by submodulees, since we don't ever rewrite those commits.
    # The call to update_refs below updates those tags.
    fast_filter_branch.do_filter(commit_filter=self.zip_filter,
                                 filter_manager=self.fm,
                                 reflist=expand_ref_pattern(self.reflist))

    # Write out the repository for input to stage2.
    print "Closing stream..."
    self.fm.close()

    self.fm = fast_filter_branch.FilterManager()

    # stage2 - We now know the topology of the zippped history.  Go
    # back and update parents of non-inlined subproject commits.  We
    # do this in a separate stage because stage1 won't see any
    # monorepo-rewritten local commits that aren't referenced by
    # submodule updates, which are exactly the commits that need
    # parents rewritten.  After stage1 those commits are attached to
    # the rewritten umbrella history.
    print "Rewriting local parents..."
    # Pass the umbrella ref again so that new parents get propagated
    # properly.  These should connect to the rewritten downstream
    # history since subproject commits were inlined above.
    fast_filter_branch.do_filter(commit_filter=self.submodule_parent_filter,
                                 filter_manager=self.fm,
                                 reflist=expand_ref_pattern(self.reflist))

    # Finally, update any tags of commits we inlined.
    if self.update_tags:
      print "Updating tags..."
      fast_filter_branch.update_refs(self.fm, ['refs/tags'],
                                     self.tag_revmap, None, None, None)

    if self.revmap_out_file:
      revmap_out = open(self.revmap_out_file + '.tmp', 'w')
      for oldrev, newrev in self.stage2_umbrella_revmap.iteritems():
        # Make sure the revs we're writing are real sha1s, not marks
        newrev = self.fm.get_mark(newrev)
        revmap_out.write('%s %s\n' % (oldrev, newrev))
      os.rename(self.revmap_out_file + '.tmp', self.revmap_out_file)

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

This tool DESTRUCTIVELY MODIFIES the umbrella branches it is run on!

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
  parser.add_argument("--downstream-prefix", metavar="REFNAME",
                      default="refs/remotes/local",
                      help="The prefix for all of the migrated refs of downstream forks (default: %(default)s)")
  args = parser.parse_args()
  Zipper(args.new_repo_prefix, args.revmap_in, args.revmap_out, args.reflist,
         args.debug, args.abort_bad_submodule, args.no_rewrite_commit_msg,
         args.subdir, args.submodule_map, args.update_tags,
         args.old_repo_prefix).run()
