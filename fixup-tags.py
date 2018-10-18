#!/usr/bin/python

# Because there's no such thing as a "tag", really, in SVN (they're
# just branches, and can have arbitrary edits), the svn2git script
# generates git branches for each of the tags in the svn tree.
#
# This script attempts to figure out if the tag is equivalent to a
# particular point in its parent tree, and converts it to an annotated
# git tag, if so.
#
# It is complicated by the llvm svn layout putting tags/branches
# inside of projects, so a single conceptual "tag" operation might
# involve multiple commits.
#
# Thus, the way it works is to check if the tree of the
# merged-in-parent of the final commit is "equivalent" to the tree of
# the tag.
#
# Equivalent here means:
# 1) All subprojects (top-level directories) that do exist in the tag
#    have the same tree hash as the parent.
# 2) Other subprojects may not be present in the tag's tree, but are
#    in the parent.
#
# If they are equivalent, the tag branch is converted to an annotated
# tag with the commit message and authorship information of that last
# commit.

# TODO: figure out what to do about the 'tags' which were treated as a
# branch and actually edited.


import subprocess
import fast_filter_branch
import re
import time

def format_raw_time(raw_time):
  int_time, tz = raw_time.split(' ')
  int_time = int(int_time)
  time_tuple = time.gmtime(int_time)
  return time.strftime("%a %b %d %H:%M:%S %Y ", time_tuple) + tz

def convert_tagref(fm, tagname, branch_rev_set):
  tag_commit = fm.get_commit(tagname)
  if tag_commit.tree == fast_filter_branch.GIT_EMPTY_TREE_HASH:
    # If we have an empty tree, just remove the tag.
    print "%s: OK: empty tree, removing" % tagname
    fm.reset_ref('refs/pre-fixup-tags/' + tagname, tagname)
    fm.reset_ref(tagname, fast_filter_branch.ALL_ZERO_HASH)
    return


  # Loop over revisions down first-parent from the tag ref, looking
  # for a revision whose last-parent is in branch_rev_set.
  #
  # The goal here is to find a candidate "main branch" revision which
  # we should consider this tag to be a tag of.
  #
  # While we're at it, we also collect all the left-parent commits
  # which are not on any branch into commits_in_tag, to put in the
  # log-message.
  tag_parent_hash_candidates = []

  commit = tag_commit
  commits_in_tag = [commit]

  while True:
    if len(commit.parents) != 1 and len(commit.parents) != 2:
      print "%s: ERR: weird commit parents." % tagname
      return

    if commit.parents[-1] in branch_rev_set:
      tag_parent_hash_candidates.append(commit.parents[-1])

    # Next commit
    if commit.parents[0] in branch_rev_set:
      break
    commit = fm.get_commit(commit.parents[0])
    commits_in_tag.append(commit)

  #print tag_parent_hash_candidates
  for tag_parent_hash in tag_parent_hash_candidates:
    # Check if the trees are equivalent, per definition at the top.
    tag_tree = fm.get_tree(tag_commit.tree)
    parent_tree = fm.get_tree(fm.get_commit(tag_parent_hash).tree)
    #print "Trees:", tag_tree, parent_tree

    if any(entry not in parent_tree.iteritems() for entry in tag_tree.iteritems()):
      # Mismatched trees -- next!
      continue

    # OKAY! Let's make the tag!
    bare_tagname = re.sub('refs/heads/svntag/', '', tagname)
    newtag = fast_filter_branch.Tag(object_hash=tag_parent_hash, name=bare_tagname,
                                    tagger=commits_in_tag[0].committer, tagger_date=commits_in_tag[0].committer_date,
                                    msg=commits_in_tag[0].msg)

    if len(commits_in_tag) > 1:
      newtag.msg += '\n--\nSVN tag also included these previous commits:\n'
    for c in commits_in_tag[1:]:
      newtag.msg += ("\n"
                     "Author: %s\n"
                     "Date: %s\n"
                     "\n"
                     "    %s\n") % (c.author, format_raw_time(c.author_date), c.msg.rstrip().replace('\n', '\n    '))

    # Backup ref, clear it, and write the tag
    fm.reset_ref('refs/pre-fixup-tags/' + tagname, tagname)
    fm.reset_ref(tagname, fast_filter_branch.ALL_ZERO_HASH)
    fm.write_tag(newtag)
    print "%s: OK: Wrote as a real tag, superseding %d commits!" % (tagname, len(commits_in_tag))
    return
  print "%s: ERR: tree not equivalent to potential parents %s" % (tagname, list(tag_parent_hash_candidates))


def main():
  fm = fast_filter_branch.FilterManager()
  refs = fast_filter_branch.list_refs()

  branches = [ref for ref in refs if not ref.startswith('refs/heads/svntag')]
  tags = [ref for ref in refs if ref.startswith('refs/heads/svntag')]

  # List all the revisions along the first-parentage of branches.
  branch_rev_set = set(subprocess.check_output(['git', 'rev-list', '--first-parent'] + branches).split('\n')[:-1])

  for tag in tags:
    convert_tagref(fm, tag, branch_rev_set)

  fm.close()

if __name__ == '__main__':
  main()
