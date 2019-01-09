#!/usr/bin/python

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

class Importer:
  """Rewrite repository history under a directory."""
  def __init__(self, new_upstream_prefix, revmap_out, subdir,
               parent_commit_hash, import_list, reflist, tag_prefix):
    if not new_upstream_prefix.endswith('/'):
      new_upstream_prefix = new_upstream_prefix + '/'

    self.import_set = set()

    # Get hashes of the import list to seed tracking which commits to
    # put alongside other monorepo blobs.  This list keeps growing as
    # we add more such commits.
    if import_list:
      if not parent_commit_hash:
        raise Exception("No parent commit given for imported refs")
      for import_ref in expand_ref_pattern(import_list):
        ref_hash = subprocess.check_output(['git', 'rev-parse'] + import_ref).split('\n')[:-1]
        self.import_set.add(ref_hash)

    self.new_upstream_prefix = new_upstream_prefix
    self.subdir = subdir
    self.reflist = reflist
    self.parent_commit_hash = parent_commit_hash
    self.revmap_out = revmap_out
    self.tag_prefix = tag_prefix

  def commit_filter(self, fm, githash, commit, oldparents):
    """Do the real filtering work..."""

    # Move the tree under the correct subdir.  If this is an imported
    # commit, preserve everything else alongside it.  Otherwise,
    # delete everything else alongside it.
    oldtree = commit.get_tree_entry()
    newtree = oldtree

    imported = False

    if commit in self.import_set:
      imported = True
      # First commit imported on this branch.  Add parent_commit as a
      # parent.
      commit.parents.extend(self.parent_commit_hash)
    else:
      for parent in oldparents:
        if parent in self.import_set:
          imported = True
          self.import_set.add(githash)
          break

    if not imported:
      # Clear all entries from newtree.
      subentries = newtree.get_subentries(fm).items()
      for name, entry in subentries:
        newtree = newtree.remove_entry(fm, name)

    # Add oldtree under subdir.
    newtree = newtree.add_entry(fm, self.subdir, oldtree)
    newtree.write_subentries(fm)
    commit.treehash = newtree.githash

    return commit

  def tag_filter(self, fm, tagobj):
    if self.tag_prefix is None:
      return tagobj

    oldname = tagobj.name
    tagobj.name = self.tag_prefix + '-' + tagobj.name

    print 'Rewriting tag %s to %s' % (oldname, tagobj.name)

    return tagobj

  def run(self):
    if self.revmap_out:
      # Only supports output, not input
      try:
        os.remove(self.revmap_out)
      except OSError:
        pass

    self.fm = fast_filter_branch.FilterManager()

    print "Importing commits..."
    fast_filter_branch.do_filter(commit_filter=self.commit_filter,
                                 tag_filter=self.tag_filter,
                                 filter_manager=self.fm,
                                 revmap_filename=self.revmap_out,
                                 reflist=expand_ref_pattern(self.reflist))
    self.fm.close()
    print "Done -- refs updated in-place."

if __name__=="__main__":
  parser = argparse.ArgumentParser(description="""
This tool assists with migrating a fork of the split-project git repositories
into a monorepo.

It will take an existing third-party repository, and translate each
such commit as if had been committed under a given directory in the
monorepo. The parent hashes will be modified to match.

The given "import-list" commits and all descendents will be rewritten
alongside all other blobs in the monorepo.  All other commits will
include *only* blobs from the third-party repository.  No attempt is
made to interleave commits descended from import-list with other
commits in the monorepo.  They will all be applied at parent-commit.

import-list functionality IS NOT WELL TESTED!  For most projects it
should be sufficient to import the project without any connection to
the existing monorepo history (don't pass --import-list), and simply
merge the resulting branch to the point in the monorepo history where
you want it.

Common values for import-list given the example below are
"refs/removes/myrepo/master," which would rewrite only the tip of
master alongside the monorepo blobs, leaving all ancestors to contain
only blobs imported from myrepo, or "$(git log -n1 --reverse
refs/removes/myrepo/master," which would rewrite all commits in the
repository alongside the monorepo (assuming the root commit of master
is reachable from all other commits).

This tool DESTRUCTIVELY MODIFIES the repository it is run on -- please
always run on a fresh clone!

Typical usage:
  # First, prepare a repository:
  mkdir myrepo-migrate && cd myrepo-migrate && git init

  git remote add new https://github.com/llvm-git-prototype/llvm.git

  git remote add myrepo https://my.repo.location/repo.git

  git fetch --all

  # Then, run this script:
  import-downstream-repo.py refs/remotes/myrepo refs/tags --subdir=myrepo

  # Then, merge the resulting branch (assuming myrepo master is what you want):
  git checkout -b mybranch new/master
  git merge myrepo/master

""",
  formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--new-repo-prefix", metavar="REFNAME", default="refs/remotes/new",
                      help="The prefix for all the refs of the new repository (default: %(default)s).")
  parser.add_argument("--subdir", metavar="DIR",
                      help="The directory under which to place the imported commits.")
  parser.add_argument("--import-list", metavar="REFNAME", default=None,
                      help="The commits to merge with other monorepo entries.")
  parser.add_argument("--parent-commit", metavar="REFNAME", default=None,
                      help="The commit on top of which the new reposiory should be synced.")
  parser.add_argument("--revmap-out", metavar="FILE", default=None)
  parser.add_argument("--tag-prefix", metavar="PREFIX", default=None,
                      help="Prefix tags with <PREFIX>")
  parser.add_argument("reflist", metavar="REFPATTERN", help="Patterns of the references to convert.", nargs='*')
  args = parser.parse_args()
  Importer(args.new_repo_prefix, args.revmap_out, args.subdir, args.parent_commit, args.import_list, args.reflist, args.tag_prefix).run()
