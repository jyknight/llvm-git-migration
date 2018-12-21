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

# Let's pretend some tag heads were different; typically someone made an extra
# commit to the tag after it was created.
SPECIAL_CASED_TAG_REPLACEMENTS = {
    # A commit added lldb and polly at a different base rev.
    'refs/heads/svntag/RELEASE_33/rc1': 'refs/heads/svntag/RELEASE_33/rc1^',
    # Added extra PPC release notes directly to the tag.
    'refs/heads/svntag/RELEASE_33/rc3': 'refs/heads/svntag/RELEASE_33/rc3^',
    # Added libunwind at a different base rev.
    'refs/heads/svntag/RELEASE_370/rc1': 'refs/heads/svntag/RELEASE_370/rc1^',
    # Added extra PPC release notes.
    'refs/heads/svntag/RELEASE_390/rc2': 'refs/heads/svntag/RELEASE_390/rc2^',
}

# Leaving unhandled: RELEASE_33/dot1-rc2; it's a mess and there was no 3.3.1 final anyways.

# Sometimes the automatic determination of the base revision for a tag fails,
# due to CVS->SVN conversion detreitus.
SPECIAL_CASED_TAG_BASES = {
    # These all were created with the wrong parent commit, but a tree matching
    # the correct parent.
    'refs/heads/svntag/RELEASE_22': 'refs/heads/release_22',
    'refs/heads/svntag/RELEASE_20': 'refs/heads/release_20',
    'refs/heads/svntag/RELEASE_19': 'refs/heads/release_19~9',
    'refs/heads/svntag/RELEASE_16': 'refs/heads/release_16',
    'refs/heads/svntag/RELEASE_15': 'refs/heads/release_15',
    # extraneous file llvm/docs/LLVMVsTheWorld.html in the tag.
    'refs/heads/svntag/RELEASE_14': 'refs/heads/release_14',
    # missing file llvm/lib/Support/ConstantRange.cpp in the tag.
    'refs/heads/svntag/RELEASE_13': 'refs/heads/release_13',
    # Wrong parent again.
    'refs/heads/svntag/RELEASE_12': 'refs/heads/release_12',
    # extraneous file llvm/docs/LLVMVsTheWorld.html
    'refs/heads/svntag/RELEASE_11': 'refs/heads/release_11',
    # extraneous file llvm/docs/ReleaseTasks.html
    'refs/heads/svntag/RELEASE_1': 'refs/heads/release_1',
}

# We rename the official release tags to "llvmorg-7.0.1-rc1" or "llvmorg-7.0.0"
def map_tagname(oldname):
  if not oldname.startswith("RELEASE_"):
    return oldname
  if oldname == 'RELEASE_342/final':
    return oldname # duplicate

  oldname = oldname[len("RELEASE_"):]
  if '/' in oldname:
    vers, kind = oldname.split('/', 1)
  else:
    vers, kind = oldname, ''

  # Handle the weird names used in 34 and 35 releases;
  # Map (34, dot1-final) -> (341, final)
  dotfoo = re.match("dot([0-9])-(.*)$", kind)
  if dotfoo:
    vers += dotfoo.group(1)
    kind = dotfoo.group(2)

  # e.g. 1 -> 100, 27 -> 270
  while len(vers) < 3:
    vers += '0'

  # Add dots to version
  vers = vers[0:-2] + "." + vers[-2:-1] + "." + vers[-1:]

  # Remove "final"
  if kind == "final":
    kind = ""

  if kind:
    return "llvmorg-" + vers + '-' + kind
  else:
    return "llvmorg-" + vers


def convert_tagref(fm, tagname, branch_rev_set):
  tag_commit = fm.get_commit(SPECIAL_CASED_TAG_REPLACEMENTS.get(tagname, tagname))
  if tag_commit.treehash == fast_filter_branch.GIT_EMPTY_TREE_HASH:
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

  # Check if we have a special case for the tag base that wouldn't be matched normally.
  selected_parent = SPECIAL_CASED_TAG_BASES.get(tagname, None)

  if selected_parent is None:
    # Normal case -- we don't have a special case, so just search.
    for tag_parent_hash in tag_parent_hash_candidates:
      # Check if the trees are equivalent, per definition at the top.
      tag_tree = fm.get_tree(tag_commit.treehash)
      parent_tree = fm.get_tree(fm.get_commit(tag_parent_hash).treehash)
      #print "Trees:", tag_tree, parent_tree

      if not any(entry not in parent_tree.iteritems() for entry in tag_tree.iteritems()):
        # Matching trees, yay!
        selected_parent = tag_parent_hash
        break

  # Did we find anything?
  if selected_parent is not None:
    # OKAY! Let's make the tag!
    new_tagname = map_tagname(re.sub('refs/heads/svntag/', '', tagname))
    newtag = fast_filter_branch.Tag(object_hash=selected_parent, name=new_tagname,
                                    tagger=tag_commit.committer, tagger_date=tag_commit.committer_date,
                                    msg=tag_commit.msg)

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
  refs = fast_filter_branch.list_branches_tags()

  branches = [ref for ref in refs if not ref.startswith('refs/heads/svntag')]
  tags = [ref for ref in refs if ref.startswith('refs/heads/svntag')]

  # List all the revisions along the first-parentage of branches.
  branch_rev_set = set(subprocess.check_output(['git', 'rev-list', '--first-parent'] + branches).split('\n')[:-1])

  for tag in tags:
    convert_tagref(fm, tag, branch_rev_set)

  fm.close()

if __name__ == '__main__':
  main()
