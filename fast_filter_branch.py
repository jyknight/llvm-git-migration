"""A replacement for git filter-branch which runs much faster, by using a
couple of techniques:
1. Works directly on tree objects, rather than going through the
   index.
2. Caches the tree modifications
3. Uses batch commands like 'git fast-import' and 'git cat-file
   --batch', instead of invoking a subprocess for every little operation.

To use, import this module from your own script, and call the function do_filter().

TODO ideas:
- Release as a standalone project.
- Add some sort of commandline interface.
- Add ability to specify refs to rewrite and to exclude.
- ...
"""

import sys
import subprocess
import threading
import collections
import os

try:
  # Attempt to use https://pypi.python.org/pypi/regex
  # for its support of partial regex matching.
  import regex
  supports_partial = True
except ImportError:
  print "WARNING: could not import regex module; falling back to re module, will be slower..."
  import re as regex
  supports_partial = False

class defaultdict(collections.defaultdict):
  __repr__ = dict.__repr__

GIT_EMPTY_TREE_HASH = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
ALL_ZERO_HASH = '0000000000000000000000000000000000000000'

def object_type(mode):
  "Convert from git mode string to a git object type"
  if mode == '40000':
    return 'tree'
  elif mode == '160000':
    return 'commit'
  else:
    return 'blob'


class Commit(object):
  """Represents one commit object from Git."""

  __slots__=['tree', 'parents', 'author', 'author_date', 'committer',
             'committer_date', 'msg']

  def __init__(self, tree=None, parents=None, author=None, author_date=None,
               committer=None, committer_date=None, msg=None):
    self.tree = tree
    self.parents = parents
    if parents is None:
      self.parents = []
    self.author=author
    self.author_date = author_date
    self.committer = committer
    self.committer_date = committer_date
    self.msg = msg

  def copy(self):
    return Commit(tree=self.tree, parents=self.parents[:], author=self.author,
                  author_date=self.author_date, committer=self.committer,
                  committer_date=self.committer_date, msg=self.msg)

  def __eq__(self, other):
    return (self.tree == other.tree and
            self.parents == other.parents and
            self.author == other.author and
            self.author_date == other.author_date and
            self.committer == other.committer and
            self.committer_date == other.committer_date and
            self.msg == other.msg)

  def __ne__(self, other):
    return not self.__eq__(other)

class Tag(object):
  """Represents one tag object from Git."""
  def __init__(self, object_hash=None, object_type=None, name=None,
               tagger=None, tagger_date=None, msg=None):
    self.object_hash = object_hash
    self.object_type = object_type
    self.name = name
    self.tagger=tagger
    self.tagger_date = tagger_date
    self.msg = msg

  def copy(self):
    return Tag(object_hash=self.object_hash, object_type=self.object_type,
               name=self.name, tagger=self.tagger,
               tagger_date=self.tagger_date, msg=self.msg)

  def __eq__(self, other):
    return (self.object_hash == other.object_hash and
            self.object_type == other.object_type and
            self.name == other.name and
            self.tagger == other.tagger and
            self.tagger_date == other.tagger_date and
            self.msg == other.msg)

  def __ne__(self, other):
    return not self.__eq__(other)

class TreeEntry(object):
  """Represents one directory/file entry in a tree object.
  Cached, thus, immutable."""
  __slots__ = ['name', 'mode', 'hash']

  def __init__(self, name, mode, hash):
    object.__setattr__(self, 'name', name)
    object.__setattr__(self, 'mode', mode)
    object.__setattr__(self, 'hash', hash)

  def __eq__(self, other):
    return (self.name == other.name and
            self.hash == other.hash and
            self.mode == other.mode)

  def __ne__(self, other):
    return not self.__eq__(other)

  def __setattr__(self, val):
    raise NotImplementedError

  def __repr__(self):
    return "TreeEntry(%r, %r, %r)" % (self.name, self.mode, self.hash)

class CatFileInput(object):
  """Runs a 'git cat-file' subprocess to allow lookup of objects in a
  git repository."""

  tree_re = regex.compile("([0-9]*) ([^\x00]*)\x00(.{20})", regex.DOTALL)

  def __init__(self):
    self.process = subprocess.Popen(['git', 'cat-file', '--batch'],
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE)

  def close(self):
    self.process.stdin.close()
    self.process.wait()
    if self.process.returncode != 0:
      raise Exception("cat-file exited with non-zero exit code:",
                      self.process.returncode)

  def _parse_object(self, githash):
    """Given a git hash, reads the object and returns (object_kind, contents)"""
    self.process.stdin.write('%s\n' % githash)
    header = self.process.stdout.readline()
    header_parts = header.split()
    if len(header_parts) != 3:
      raise Exception("Unexpected response from cat-file", githash, header)

    response = self.process.stdout.read(int(header_parts[2]))
    if self.process.stdout.read(1) != "\n":
      raise Exception("Missing expected terminating newline from cat-file.")

    return header_parts[1], response

  def parse_tree(self, githash):
    """Given a git hash representing a tree object, returns the list of
    'TreeEntry's in that tree."""
    files = []
    kind, response = self._parse_object(githash)
    if kind != "tree":
      raise Exception("Unexpected object kind: %r is a %r not a tree",
                      githash, kind)

    last_pos = 0
    for entry in self.tree_re.finditer(response):
      if last_pos != entry.start():
        raise Exception("Unexpected tree content", last_pos, entry.start(),
                        response[last_pos:entry.start()+1])
      last_pos = entry.end()
      files.append(TreeEntry(entry.group(2), entry.group(1),
                             entry.group(3).encode('hex')))

    if last_pos != len(response):
      raise Exception("Junk at end of tree?", githash, last_pos, len(response))
    return files

  def parse_commit(self, githash):
    """Given a git hash representing a commit object, returns a 'Commit' class
    representing the commit."""
    commit = Commit()

    kind, response = self._parse_object(githash)
    if kind != "commit":
      Exception("Unexpected object kind: %r is a %r not a commit", githash, kind)

    headers, commit.msg = response.split('\n\n', 1)
    for header in headers.split('\n'):
      header_kind, header_data = header.split(' ', 1)

      if header_kind == 'tree':
        commit.tree = header_data
      elif header_kind == 'parent':
        commit.parents.append(header_data)
      elif header_kind == 'author':
        commit.author, commit.author_date = header_data.split('> ', 1)
        commit.author = commit.author + '>'
      elif header_kind == 'committer':
        commit.committer, commit.committer_date = header_data.split('> ', 1)
        commit.committer = commit.committer + '>'
      else:
        raise Exception("Unexpected commit header", header)

    return commit

  def parse_tag(self, githash):
    tag = Tag()

    kind, response = self._parse_object(githash)
    if kind != "tag":
      Exception("Unexpected object kind: %r is a %r not a commit", githash, kind)

    headers, tag.msg = response.split('\n\n', 1)
    for header in headers.split('\n'):
      header_kind, header_data = header.split(' ', 1)

      if header_kind == 'object':
        tag.object_hash = header_data
      elif header_kind == 'type':
        tag.object_type = header_data
      elif header_kind == 'tag':
        tag.name = header_data
      elif header_kind == 'tagger':
        tag.tagger, tag.tagger_date = header_data.split('> ', 1)
        tag.tagger = tag.tagger + '>'
      else:
        raise Exception("Unexpected tag header", header)

    return tag

  def get_object_type(self, githash):
    kind, response = self._parse_object(githash)
    return kind

class FastImportStream(object):
  """Runs a "git fast-import" subprocess to allow importing objects into
  a git repository."""
  tmp_refname = 'refs/xxxx-fast-filter-tmp-ref'
  def __init__(self):
    self.process = subprocess.Popen(['git', 'fast-import', '--force',
                                     '--date-format=raw', '--done'],
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE)
    self.next_mark = 1

  def close(self):
    # Delete the temporary refname we added
    self.reset_ref(self.tmp_refname, ALL_ZERO_HASH)
    # Close everything down
    self.process.stdin.write("done\n")
    self.process.stdin.close()
    self.process.wait()
    if self.process.returncode:
      raise Exception("fast-import exited with non-zero exit code:",
                      self.process.returncode)

  def write_commit(self, commit):
    """Given an object of the 'Commit' class, write it to the git repository.
    Returns the mark representing the commit (which can be used as the parent
    of other commits)."""
    mark = self.next_mark
    self.next_mark += 1
    s = ("commit %s\n"
         "mark :%d\n"
         "author %s %s\n"
         "committer %s %s\n"
         "data %d\n"
         "%s\n"
         "from %s\n"
         ) % (self.tmp_refname, mark, commit.author, commit.author_date,
              commit.committer, commit.committer_date, len(commit.msg),
              commit.msg, ALL_ZERO_HASH)
    for p in commit.parents:
        s += "merge %s\n" % p
    s += "M 40000 %s \n\n" % commit.tree
    self.process.stdin.write(s)
    return ":%d" % mark

  def write_tag(self, tag):
    s = ("tag %s\n"
         "from %s\n"
         "tagger %s %s\n"
         "data %d\n"
         "%s\n") % (
             tag.name, tag.object_hash, tag.tagger, tag.tagger_date,
             len(tag.msg), tag.msg)
    self.process.stdin.write(s)

  def reset_ref(self, ref, commit):
    """Sets the named 'ref' to point to the named 'commit'. (Can set it to
    a hash or a mark)"""
    self.process.stdin.write("reset %s\nfrom %s\n\n" % (ref, commit))

  def get_mark(self, mark):
    """Returns the SHA1 corresponding to a mark"""
    self.process.stdin.write("get-mark %s\n" % (mark,))
    return self.process.stdout.readline().rstrip()

class TreeImportStream(object):
  """Runs a 'git mktree' subprocess to create trees without commits
  (which fast-import can't do by itself unfortunately)."""
  def __init__(self):
    self.process = subprocess.Popen(['git', 'mktree', '-z', '--batch'],
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE)

  def close(self):
    self.process.stdin.close()
    self.process.wait()
    if self.process.returncode != 0:
      raise Exception("mktree exited with non-zero exit code:",
                      self.process.returncode)

  def write_tree(self, files):
    """Given a list of 'TreeEntry's, create a git tree object, and return
    its hash."""
    s = '\x00'.join('%s %s %s\t%s' % (f.mode, object_type(f.mode),
                                      f.hash, f.name)
                    for f in files)
    s += '\x00\x00'
    self.process.stdin.write(s)
    return self.process.stdout.readline().strip()

class FilterManager(object):
  """Wrapper for the above git import/read functionality."""
  def __init__(self):
    self._cat_file=CatFileInput()
    self._tree_import=TreeImportStream()
    self._fast_import = FastImportStream()

    self._cached_trees = {}
    self._cached_commits = {}

  def close(self):
    self._cat_file.close()
    self._tree_import.close()
    self._fast_import.close()

  def get_tree(self, githash):
    """Returns a list of 'TreeEntry's in a tree, given a git hash. Caches
    the result."""
    t = self._cached_trees.get(githash)
    if t is not None:
      return t
    t = self._cat_file.parse_tree(githash)
    self._cached_trees[githash] = t
    return t

  def get_commit(self, githash):
    commit = self._cached_commits.get(githash)
    if commit is not None:
      return commit
    commit = self._cat_file.parse_commit(githash)
    self._cached_commits[githash] = commit
    return commit.copy()

  def get_tag(self, githash):
    return self._cat_file.parse_tag(githash)

  def get_mark(self, mark):
    """Returns the SHA1 corresponding to a mark"""
    return self._fast_import.get_mark(mark)

  def write_tree(self, entries):
    """Writes a git tree given a list of 'TreeEntry's. Returns the hash,
    and caches it."""
    githash = self._tree_import.write_tree(entries)
    self._cached_trees[githash] = entries
    return githash

  def write_commit(self, commit):
    mark = self._fast_import.write_commit(commit)
    self._cached_commits[mark] = commit
    return mark

  def write_tag(self, tag):
    return self._fast_import.write_tag(tag)

  def reset_ref(self, ref, githash):
    self._fast_import.reset_ref(ref, githash)

UNSET = object()
class GlobalTreeTransformer(object):
  """Utility to transform files in a tree, based only on the existing
  contents, not on which commit points to it.

  Give the constructor a list of changes you want to make:
  [(PATH, ACTION), ...]

  Where "PATH" is a regex to match the full pathname -- ending with a
  slash if the final component to act upon is a directory. ACTION
  should be "None" to delete the file entry, or a function to edit the
  contents.
  """

  def __init__(self, manager, file_changes, prefix_sensitive=True):
    self.manager = manager
    # Map from (prefix_path, tree_hash) -> new_tree_hash
    self._mapping = {}

    self._matchers_prefix_sensitive = False
    self._transforms_prefix_sensitive = prefix_sensitive
    for (path,action) in file_changes:
      if not path.startswith('.*'):
        self._matchers_prefix_sensitive = True
    self._transforms = [(regex.compile(path+'$'), action)
                        for (path, action) in file_changes]

    self._stat_tree_cache_hits = 0
    self._stat_wrote_trees = 0
    self._stat_got_trees = 0
    self._stat_transforms = 0

  def dump_stats(self):
    print "GlobalTreeTransformer statistics:"
    print "  Tree cache hits:   %8d" % self._stat_tree_cache_hits
    print "  Trees retrieved:   %8d" % self._stat_got_trees
    print "  Trees written:     %8d" % self._stat_wrote_trees
    print "  Transforms called: %8d" % self._stat_transforms

  def transform(self, oldtreehash):
    finaltree = self._transform_internal(
        '/', oldtreehash, self._transforms,
        self._matchers_prefix_sensitive or self._transforms_prefix_sensitive)
    if finaltree is None:
      return GIT_EMPTY_TREE_HASH
    return finaltree

  def _transform_internal(self, prefix, oldtreehash, cur_transforms, cur_prefix_sensitive):
    if cur_prefix_sensitive:
      cache_prefix = prefix
    else:
      cache_prefix = None
    treehash = self._mapping.get((cache_prefix, oldtreehash), UNSET)
    if treehash is not UNSET:
      self._stat_tree_cache_hits += 1
      return treehash

    treehash = oldtreehash

    if supports_partial:
      # We're using the regex module, so we get partial match support.
      sub_transforms = []
      sub_prefix_sensitive = self._transforms_prefix_sensitive
      for t in cur_transforms:
        m = t[0].match(prefix, partial=True)
        if m is not None:
          if not t[0].pattern.startswith('.*'):
            sub_prefix_sensitive = True
          if m.partial:
            sub_transforms.append(t)
          else:
            treehash = self.fulltree_transform_callback(prefix, treehash, t)
    else:
      # The 're' module doesn't support partial matches, so we can't
      # filter regexes as we go. Oh well.
      sub_prefix_sensitive = cur_prefix_sensitive
      sub_transforms = cur_transforms
      for t in cur_transforms:
        m = t[0].match(prefix)
        if m is not None:
          treehash = self.fulltree_transform_callback(prefix, treehash, t)

    if sub_transforms and treehash is not None:
      self._stat_got_trees += 1
      old_entries = self.manager.get_tree(treehash)
      new_entries = self.entries_transform_callback(prefix, old_entries,
                                                    sub_transforms, sub_prefix_sensitive)

      if not new_entries:
        treehash = None
      elif new_entries != old_entries:
        self._stat_wrote_trees += 1
        treehash = self.manager.write_tree(new_entries)

    self._mapping[(cache_prefix, oldtreehash)] = treehash
    return treehash

  def fulltree_transform_callback(self, pathname, oldtreehash, t):
    if t[1] is None:
      return None
    else:
      self._stat_transforms += 1
      return t[1](pathname, oldtreehash)

  def entries_transform_callback(self, prefix, entries, transform_list, prefix_sensitive):
    result = []
    for entry in entries:
      if entry.mode == '40000':
        newtreehash = self._transform_internal(prefix + entry.name + '/',
                                               entry.hash, transform_list, prefix_sensitive)
        if newtreehash is None:
          pass
        elif newtreehash == entry.hash:
          result.append(entry)
        else:
          result.append(TreeEntry(entry.name, entry.mode, newtreehash))
      else:
        fullname = prefix + entry.name
        entryhash = entry.hash
        for t in transform_list:
          if t[0].match(fullname):
            if t[1] is None:
              entryhash = None
            else:
              self._stat_transforms += 1
              entryhash = t[1](fullname, entryhash)

            if entryhash is None:
              break
        else:
          if entryhash == entry.hash:
            result.append(entry)
          else:
            result.append(TreeEntry(entry.name, entry.mode, entryhash))
    return result


def list_refs():
  return subprocess.check_output(['git', '-c', 'core.warnAmbiguousRefs=false', 'rev-parse', '--symbolic-full-name',
                                  '--branches', '--tags']).split('\n')[:-1]

def update_refs(fm, reflist, revmap, backup_prefix, tag_filter, msg_filter):
  print "Updating refs..."

  proc = subprocess.Popen(['git', 'for-each-ref'] + reflist,
                          stdout=subprocess.PIPE)
  for line in proc.stdout:
    line = line.rstrip('\n')
    githash_and_kind, refname = line.split('\t', 1)
    githash, kind = githash_and_kind.split(' ')
    if kind == 'commit':
      if githash in revmap:
        print "Updating REF %s %s -> %s" % (refname, githash, revmap[githash])
        if backup_prefix:
          # Create backup of original ref
          fm.reset_ref(backup_prefix + '/' + refname, githash)
        # Reset to new commit
        fm.reset_ref(refname, revmap[githash])
    elif kind == 'tag':
      tagobj = fm.get_tag(githash)
      if "refs/tags/" + tagobj.name != refname:
        print "WARNING: tag %s has mismatched tagname: %s" % (
            refname, tagobj.name)
        continue

      if tagobj.object_type != "commit":
        print "WARNING: tag %s points to %s, not to a commit" % (
            refname, tagobj.object_type)
        continue

      # Strip the signature -- and do this before storing in oldtagobj
      # for comparison, so that we're only rewriting the tag if there
      # are OTHER changes.
      was_signed = False
      if '\n-----BEGIN PGP SIGNATURE-----\n' in tagobj.msg:
        tagobj.msg = tagobj.msg.split('\n-----BEGIN PGP SIGNATURE-----\n')[0]
        was_signed = True
      oldtagobj = tagobj.copy()

      if tagobj.object_hash in revmap:
        tagobj.object_hash = revmap[tagobj.object_hash]

      if msg_filter is not None:
        tagobj.msg = msg_filter(tagobj.msg)
      if tag_filter is not None:
        tagobj = tag_filter(tagobj)

      if tagobj != oldtagobj:
        print "Updating TAG %s" % (refname,)
        if backup_prefix:
          # Create backup ref
          fm.reset_ref(backup_prefix + '/' + refname, githash)
        fm.write_tag(tagobj)
        if was_signed:
          print "WARNING: stripped signature from tag %s (%s)" % (
              tagname, githash)
    else:
      raise Exception("Unexpected ref to kind", kind)

  proc.wait()
  if proc.returncode != 0:
    raise Exception("for-each-ref exited with non-zero exit code:",
                    proc.returncode)

def do_filter(commit_filter=None, tag_filter=None, global_file_actions=None,
              prefix_sensitive=True, msg_filter=None,
              backup_prefix="refs/original", revmap_filename=None):
  fm = FilterManager()

  if global_file_actions:
    gtt = GlobalTreeTransformer(fm, global_file_actions, prefix_sensitive)
  else:
    gtt = None

  reflist = list_refs()
  print "Getting list of commits..."
  # Get list of commits to work on:
  revlist = subprocess.check_output(['git', 'rev-list', '--reverse',
                                     '--topo-order'] + reflist).split('\n')[:-1]

  if revmap_filename and os.path.exists(revmap_filename):
    revmap = dict(l.strip().split(' ') for l in open(revmap_filename, 'r'))
  else:
    revmap={}

  print "Filtering..."
  progress = 0
  for rev in revlist:
    if progress % 100 == 0:
      print " [%d/%d]\r" % (progress, len(revlist)),

    if rev in revmap:
      # If this commit was already processed (with an input revmap), skip
      continue

    oldcommit = fm._cat_file.parse_commit(rev)
    commit = oldcommit.copy()
    commit.parents = [revmap.get(p, p) for p in commit.parents]

    if msg_filter is not None:
      commit.msg = msg_filter(commit.msg)
    if gtt is not None:
      commit.tree = gtt.transform(commit.tree)
    if commit_filter is not None:
      commit = commit_filter(fm, commit)

    if commit != oldcommit:
      revmap[rev] = fm.write_commit(commit)
    progress += 1

  update_refs(fm, reflist, revmap, backup_prefix, tag_filter, msg_filter)

  if revmap_filename:
    revmap_out = open(revmap_filename+'.tmp', 'w')
    for oldrev, newrev in revmap.iteritems():
      # Make sure the revs we're writing are real sha1s, not marks
      if newrev.startswith(':'):
        newrev = fm.get_mark(newrev)
    revmap_out.write("%s %s\n" % (oldrev, newrev))

  if gtt is not None:
    gtt.dump_stats()
  print "Filtered %d commits, %d were changed." % (len(revlist), len(revmap))
  fm.close()

  if revmap_filename:
    os.rename(revmap_filename+'.tmp', revmap_filename)
