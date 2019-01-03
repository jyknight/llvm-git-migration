"""A replacement for git filter-branch which runs much faster, by using a
couple of techniques:
1. Works directly on tree objects, rather than going through the
   index.
2. Caches the tree modifications
3. Uses batch commands like 'git fast-import' and 'git cat-file
   --batch', instead of invoking a subprocess for every little operation.

To use, import this module from your own script, and call the function
do_filter().

TODO ideas:
- Release as a standalone project.
- Add some sort of commandline interface.
- Add ability to specify refs to rewrite and to exclude.
- ...

"""

import collections
import os
import subprocess
import sys

try:
  # Attempt to use https://pypi.python.org/pypi/regex
  # for its support of partial regex matching.
  import regex
  supports_partial = True
except ImportError:
  #print ('WARNING: could not import regex module; falling back to re module, '
  #       'will be slower...')
  import re as regex
  supports_partial = False


class defaultdict(collections.defaultdict):
  __repr__ = dict.__repr__


GIT_EMPTY_TREE_HASH = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
ALL_ZERO_HASH = '0000000000000000000000000000000000000000'

def object_type_from_mode(mode):
  """Convert from git mode string to a git object type"""
  if mode == '40000':
    return 'tree'
  elif mode == '160000':
    return 'commit'
  else:
    return 'blob'


class Commit(object):
  """Represents one commit object from Git."""

  __slots__ = ['treehash', 'parents', 'author', 'author_date', 'committer',
               'committer_date', 'msg']

  def __init__(self, treehash=None, parents=None, author=None, author_date=None,
               committer=None, committer_date=None, msg=None):
    self.treehash = treehash
    self.parents = parents
    if parents is None:
      self.parents = []
    self.author = author
    self.author_date = author_date
    self.committer = committer
    self.committer_date = committer_date
    self.msg = msg

  def copy(self):
    return Commit(treehash=self.treehash, parents=self.parents[:], author=self.author,
                  author_date=self.author_date, committer=self.committer,
                  committer_date=self.committer_date, msg=self.msg)

  def __eq__(self, other):
    return (self.treehash == other.treehash and
            self.parents == other.parents and
            self.author == other.author and
            self.author_date == other.author_date and
            self.committer == other.committer and
            self.committer_date == other.committer_date and
            self.msg == other.msg)

  def __ne__(self, other):
    return not self.__eq__(other)

  def get_tree_entry(self):
    return TreeEntry('40000', self.treehash)

class Tag(object):
  """Represents one tag object from Git."""

  def __init__(self, object_hash=None, object_type=None, name=None,
               tagger=None, tagger_date=None, msg=None):
    self.object_hash = object_hash
    self.object_type = object_type
    self.name = name
    self.tagger = tagger
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
  __slots__ = ['mode', 'githash', '_sub_entries']

  def __init__(self, mode, githash=None, sub_entries=None):
    assert isinstance(sub_entries, (type(None), dict))
    if sub_entries is None and githash is None:
      raise ValueError("TreeEntry requires one of githash or sub_entries arguments")
    if sub_entries is not None and mode != '40000':
      raise ValueError("TreeEntry can't have sub_entries on a non-directory.")

    object.__setattr__(self, 'mode', mode)
    object.__setattr__(self, 'githash', githash)
    object.__setattr__(self, '_sub_entries', sub_entries)

  def __eq__(self, other):
    # Note: don't consider two equal if they don't have a githash.
    if self is other:
      return True
    return (self.mode == other.mode and
            self.githash is not None and
            other.githash is not None and
            self.githash == other.githash)

  def __ne__(self, other):
    return not self.__eq__(other)

  def __setattr__(self, name, val):
    raise NotImplementedError

  def __repr__(self):
    return 'TreeEntry(%r, %r)' % (self.mode, self.githash)

  def get_subentries(self, fm):
    if self.mode != '40000':
      raise ValueError("TreeEntry can't have sub_entries on a non-directory.")
    if self._sub_entries is None:
      object.__setattr__(self, '_sub_entries', fm.get_tree(self.githash))
    return self._sub_entries

  def write_subentries(self, fm):
    if self.githash is None:
      # Write out modified subtrees if needed.
      to_remove = []
      for name, e in self._sub_entries.iteritems():
        if e.githash is None:
          e.write_subentries(fm)
        if e.githash == GIT_EMPTY_TREE_HASH:
          to_remove.append(name)

      # Filter empty subtrees
      for name in to_remove:
        del self._sub_entries[name]

      # Then, write out myself.
      object.__setattr__(self, 'githash', fm.write_tree(self._sub_entries))

  def remove_entry(self, fm, name):
    assert '/' not in name

    r = self.get_subentries(fm)
    if name not in r:
      return self

    if self.githash is None:
      del self._sub_entries[name]
      return self
    else:
      r = r.copy()
      del r[name]
      return TreeEntry(self.mode, sub_entries = r)

  def add_entry(self, fm, name, entry):
    assert '/' not in name
    if self.githash is None:
      self._sub_entries[name] = entry
      return self
    else:
      r = self.get_subentries(fm).copy()
      r[name] = entry
      return TreeEntry(self.mode, sub_entries = r)

  def get_path(self, fm, pathsegs):
    cur = self
    for ps in pathsegs:
      if cur.mode != '40000':
        return None
      cur = cur.get_subentries(fm).get(ps, None)
      if cur is None:
        return None
    return cur

  def remove_path(self, fm, pathsegs):
    if len(pathsegs) == 1:
      return self.remove_entry(fm, pathsegs[0])

    r = self.get_subentries(fm)
    oldsub = r.get(pathsegs[0])
    if oldsub is None:
      return self
    newsub = oldsub.remove_path(fm, pathsegs[1:])
    if newsub is oldsub:
      return self
    return self.add_entry(fm, pathsegs[0], newsub)

  def add_path(self, fm, pathsegs, newentry):
    if len(pathsegs) == 1:
      return self.add_entry(fm, pathsegs[0], newentry)

    r = self.get_subentries(fm)
    if pathsegs[0] in r:
      oldsub = r[pathsegs[0]]
      newsub = oldsub.add_path(fm, pathsegs[1:], newentry)
      if newsub is not oldsub:
        return self.add_entry(fm, pathsegs[0], newsub)
      return self

    newsub = TreeEntry('40000', sub_entries={})
    return self.add_entry(
        fm, pathsegs[0],
        newsub.add_path(fm, pathsegs[1:], newentry))


class CatFileInput(object):
  """Runs a 'git cat-file' subprocess to allow lookup of objects in a
  git repository."""

  tree_re = regex.compile('([0-9]*) ([^\x00]*)\x00(.{20})', regex.DOTALL)

  def __init__(self):
    self.process = subprocess.Popen(['git', 'cat-file', '--batch'],
                                    stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE)

  def close(self):
    self.process.stdin.close()
    self.process.wait()
    if self.process.returncode != 0:
      raise Exception('cat-file exited with non-zero exit code:',
                      self.process.returncode)

  def _parse_object(self, githash):
    """Given a git hash, reads the object and returns (object_kind, contents)"""
    self.process.stdin.write('%s\n' % githash)
    header = self.process.stdout.read(40) + self.process.stdout.readline()
    header_parts = header.split()
    if len(header_parts) != 3:
      raise Exception('Unexpected response from cat-file', githash, header)

    response = self.process.stdout.read(int(header_parts[2]))
    if self.process.stdout.read(1) != '\n':
      raise Exception('Missing expected terminating newline from cat-file.')

    return header_parts[1], response

  def parse_tree(self, githash):
    """Given a git hash representing a tree object, returns the dict of
    (str:TreeEntry) in that tree."""
    files = {}
    kind, response = self._parse_object(githash)
    if kind != 'tree':
      raise Exception('Unexpected object kind: %r is a %r not a tree',
                      githash, kind)

    last_pos = 0
    for entry in self.tree_re.finditer(response):
      if last_pos != entry.start():
        raise Exception('Unexpected tree content', last_pos, entry.start(),
                        response[last_pos:entry.start()+1])
      last_pos = entry.end()
      files[entry.group(2)] = TreeEntry(entry.group(1),
                                        entry.group(3).encode('hex'))

    if last_pos != len(response):
      raise Exception('Junk at end of tree?', githash, last_pos, len(response))
    return files

  def parse_commit(self, githash):
    """Given a git hash representing a commit object, returns a 'Commit' class
    representing the commit."""
    commit = Commit()

    kind, response = self._parse_object(githash)
    if kind != 'commit':
      Exception('Unexpected object kind: %r is a %r not a commit',
                githash, kind)

    headers, commit.msg = response.split('\n\n', 1)
    for header in headers.split('\n'):
      if header[0] == ' ':
        # Continuation line -- only relevant at the moment for gpgsig, which we
        # ignore.
        continue
      header_kind, header_data = header.split(' ', 1)

      encoding = None
      if header_kind == 'tree':
        commit.treehash = header_data
      elif header_kind == 'parent':
        commit.parents.append(header_data)
      elif header_kind == 'author':
        commit.author, commit.author_date = header_data.split('> ', 1)
        commit.author = commit.author + '>'
      elif header_kind == 'committer':
        commit.committer, commit.committer_date = header_data.split('> ', 1)
        commit.committer = commit.committer + '>'
      elif header_kind == 'encoding':
        encoding = header_data
      elif header_kind == 'gpgsig':
        # Ignore gpgsig headers -- if we rewrite the commit, it's impossible to
        # re-sign it, anyways.
        pass
      else:
        raise Exception('Unexpected commit header', header)

    if encoding is not None:
      # I'll just eagerly re-encode commit messages from the source encoding
      # into utf-8, as git-fast-import cannot handle non-utf8 encodings.
      msg_unicode = commit.msg.decode(encoding, error='replace')
      commit.msg = msg_unicode.encode('utf-8')

    return commit

  def parse_tag(self, githash):
    tag = Tag()

    kind, response = self._parse_object(githash)
    if kind != 'tag':
      Exception('Unexpected object kind: %r is a %r not a commit',
                githash, kind)

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
        raise Exception('Unexpected tag header', header)

    return tag

  def parse_blob(self, githash):
    kind, response = self._parse_object(githash)
    if kind != 'blob':
      raise Exception('Unexpected object kind: %r is a %r not a blob',
                      githash, kind)
    return response

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
    self.process.stdin.write('done\n')
    self.process.stdin.close()
    self.process.wait()
    if self.process.returncode:
      raise Exception('fast-import exited with non-zero exit code:',
                      self.process.returncode)

  def write_commit(self, commit):
    """Given an object of the 'Commit' class, write it to the git repository.
    Returns the mark representing the commit (which can be used as the parent
    of other commits)."""
    mark = self.next_mark
    self.next_mark += 1
    s = ('commit %s\n'
         'mark :%d\n'
         'author %s %s\n'
         'committer %s %s\n'
         'data %d\n'
         '%s\n'
         'from %s\n'
         ) % (self.tmp_refname, mark, commit.author, commit.author_date,
              commit.committer, commit.committer_date, len(commit.msg),
              commit.msg, ALL_ZERO_HASH)
    for p in commit.parents:
        s += 'merge %s\n' % p
    s += 'M 40000 %s \n\n' % commit.treehash
    self.process.stdin.write(s)
    return ':%d' % mark

  def write_tag(self, tag):
    s = ('tag %s\n'
         'from %s\n'
         'tagger %s %s\n'
         'data %d\n'
         '%s\n') % (
             tag.name, tag.object_hash, tag.tagger, tag.tagger_date,
             len(tag.msg), tag.msg)
    self.process.stdin.write(s)

  def reset_ref(self, ref, commit):
    """Sets the named 'ref' to point to the named 'commit'. (Can set it to
    a hash or a mark)"""
    self.process.stdin.write('reset %s\nfrom %s\n\n' % (ref, commit))

  def get_mark(self, mark):
    """Returns the SHA1 corresponding to a mark"""
    self.process.stdin.write('get-mark %s\n' % (mark,))
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
      raise Exception('mktree exited with non-zero exit code:',
                      self.process.returncode)

  def write_tree(self, files):
    """Given a dict of (str:TreeEntry) create a git tree object, and return
    its hash."""
    if files:
      s = '\x00'.join('%s %s %s\t%s' % (f.mode, object_type_from_mode(f.mode),
                                        f.githash, name)
                      for name,f in files.iteritems())
      s += '\x00\x00'
    else:
      s = '\x00'
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

  def get_blob(self, githash):
    return self._cat_file.parse_blob(githash)

  def get_mark(self, mark):
    """Returns the SHA1 corresponding to a mark"""
    if mark.startswith(':'):
      return self._fast_import.get_mark(mark)
    return mark

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


class _TreeTransformerBase(object):
  """Utility to transform files in a tree, based only on the existing
  contents, not on which commit points to it.

  file_changes should be a list of changes you want to make:
  [(PATH_RE, ACTION), ...]

  Where "PATH_RE" is a regex to match the full pathname -- ending with
  a slash if the final component to act upon is a directory. ACTION
  should be a function ``f(path, TreeEntry) -> TreeEntry``. That is,
  given a path and a TreeEntry, returns a transformed entry.
  """

  def __init__(self, manager, file_changes, prefix_sensitive=True):
    self.manager = manager

    self._matchers_prefix_sensitive = False
    self._transforms_prefix_sensitive = prefix_sensitive
    for (path_re, action) in file_changes:
      if not path_re.startswith('.*'):
        self._matchers_prefix_sensitive = True

    self._transforms = [(regex.compile(path_re + '$'), action)
                        for (path_re, action) in file_changes]

    self._stat_tree_cache_hits = 0
    self._stat_wrote_trees = 0
    self._stat_got_trees = 0
    self._stat_transforms = 0

  def dump_stats(self):
    print 'GlobalTreeTransformer statistics:'
    print '  Tree cache hits:   %8d' % self._stat_tree_cache_hits
    print '  Trees retrieved:   %8d' % self._stat_got_trees
    print '  Trees written:     %8d' % self._stat_wrote_trees
    print '  Transforms called: %8d' % self._stat_transforms

  def transform(self, oldtreehash):
    oldtree = TreeEntry('40000', oldtreehash)
    finaltree = self._transform_internal(
        '/', oldtree, self._transforms,
        self._matchers_prefix_sensitive or self._transforms_prefix_sensitive)
    if finaltree is None:
      return GIT_EMPTY_TREE_HASH

    finaltree.write_subentries(self.manager)
    return finaltree.githash


  def _transform_internal(self, prefix, oldtree, cur_transforms,
                          cur_prefix_sensitive):
    tree = oldtree

    if supports_partial:
      # We're using the regex module, so we get partial match support.
      sub_transforms = []
      sub_prefix_sensitive = self._transforms_prefix_sensitive
      for t in cur_transforms:
        path_re, action = t
        m = path_re.match(prefix, partial=True)
        if m is not None:
          if not path_re.pattern.startswith('.*'):
            sub_prefix_sensitive = True
          if m.partial:
            sub_transforms.append(t)
          else:
            tree = self.invoke_transform_callback(prefix, tree, action)
    else:
      # The 're' module doesn't support partial matches, so we can't
      # filter regexes as we go. Oh well.
      sub_prefix_sensitive = cur_prefix_sensitive
      sub_transforms = cur_transforms
      for t in cur_transforms:
        path_re, action = t
        m = path_re.match(prefix)
        if m is not None:
          tree = self.invoke_transform_callback(prefix, tree, action)

    if sub_transforms and tree is not None:
      self._stat_got_trees += 1
      tree = self.entries_transform_callback(prefix, tree,
                                             sub_transforms,
                                             sub_prefix_sensitive)

    return tree

  def entries_transform_callback(self, prefix, tree, transform_list,
                                 prefix_sensitive):
    modified = False
    newtree = tree

    for name, entry in tree.get_subentries(self.manager).items():
      if entry.mode == '40000':
        newentry = self._transform_internal(prefix + name + '/',
                                            entry, transform_list,
                                            prefix_sensitive)
      else:
        fullname = prefix + name
        newentry = entry
        for t in transform_list:
          path_re, action = t
          if path_re.match(fullname):
            newentry = self.invoke_transform_callback(fullname, newentry, action)
            if newentry is None:
              break

      if newentry is None:
        newtree = newtree.remove_entry(self.manager, name)
      elif newentry != entry:
        newtree = newtree.add_entry(self.manager, name, newentry)

    return newtree

  def invoke_transform_callback(self, pathname, oldtree, action):
    self._stat_transforms += 1
    return action(self.manager, pathname, oldtree)
    # if res is oldtree:
    #   print "Transform for", pathname, "same tree", action
    # else:
    #   print "Transform for", pathname, "NEW tree", action
    #   print "OLD::::"
    #   print oldtree and oldtree.githash
    #   print oldtree and oldtree._sub_entries
    #   print "NEW::::"
    #   print res and res.githash
    #   print res and res._sub_entries
    # return res

class CachingTreeTransformer(_TreeTransformerBase):
  def __init__(self, manager, file_changes=[], prefix_sensitive=True):
    _TreeTransformerBase.__init__(self, manager, file_changes, prefix_sensitive)

    # Map from (prefix_path, tree_hash) -> new_tree_hash
    self._mapping = {}

  def _transform_internal(self, prefix, oldtree, cur_transforms, cur_prefix_sensitive):
    if cur_prefix_sensitive:
      cache_prefix = prefix
    else:
      cache_prefix = None
    assert oldtree.mode == '40000'
    assert oldtree.githash
    tree = self._mapping.get((cache_prefix, oldtree.githash), UNSET)
    if tree is not UNSET:
      self._stat_tree_cache_hits += 1
      return tree

    tree = _TreeTransformerBase._transform_internal(self, prefix, oldtree, cur_transforms, cur_prefix_sensitive)

    # Make immutable
    if tree is not None:
      tree.write_subentries(self.manager)
    self._mapping[(cache_prefix, oldtree.githash)] = tree
    return tree

def list_branches_tags():
  return subprocess.check_output(['git', '-c', 'core.warnAmbiguousRefs=false',
                                  'rev-parse', '--symbolic-full-name',
                                  '--branches', '--tags']).split('\n')[:-1]

def update_refs(fm, reflist, revmap, backup_prefix, tag_filter, msg_filter):
  print 'Updating refs...'

  proc = subprocess.Popen(['git', 'for-each-ref'] + reflist,
                          stdout=subprocess.PIPE)
  for line in proc.stdout:
    line = line.rstrip('\n')
    githash_and_kind, refname = line.split('\t', 1)
    githash, kind = githash_and_kind.split(' ')
    if kind == 'commit':
      if githash in revmap:
        print 'Updating REF %s %s -> %s' % (refname, githash, revmap[githash])
        if backup_prefix:
          # Create backup of original ref
          fm.reset_ref(backup_prefix + '/' + refname, githash)
        # Reset to new commit
        fm.reset_ref(refname, revmap[githash])
    elif kind == 'tag':
      tagobj = fm.get_tag(githash)
      if 'refs/tags/' + tagobj.name != refname:
        print 'WARNING: tag %s has mismatched tagname: %s' % (
            refname, tagobj.name)
        continue

      if tagobj.object_type != 'commit':
        print 'WARNING: tag %s points to %s, not to a commit' % (
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
        tagobj = tag_filter(fm, tagobj)

      if tagobj != oldtagobj:
        print 'Updating TAG %s' % (refname,)
        if backup_prefix:
          # Create backup ref
          fm.reset_ref(backup_prefix + '/' + refname, githash)
        fm.write_tag(tagobj)
        if was_signed:
          print 'WARNING: stripped signature from tag %s (%s)' % (
              refname, githash)
    else:
      raise Exception('Unexpected ref to kind', kind)

  proc.wait()
  if proc.returncode != 0:
    raise Exception('for-each-ref exited with non-zero exit code:',
                    proc.returncode)

def do_filter(commit_filter=None, tag_filter=None, global_file_actions=None,
              prefix_sensitive=True, msg_filter=None,
              backup_prefix='refs/original', revmap_filename=None, reflist=None,
              filter_manager=None):
  if filter_manager:
    fm = filter_manager
  else:
    fm = FilterManager()

  if global_file_actions:
    gtt = CachingTreeTransformer(fm, file_changes=global_file_actions, prefix_sensitive=prefix_sensitive)
  else:
    gtt = None

  if reflist is None:
    reflist = list_branches_tags()

  print 'Getting list of commits...'
  # Get list of commits to work on:
  revlist = subprocess.check_output(['git', 'rev-list', '--reverse',
                                     '--topo-order'] + reflist).split('\n')[:-1]

  if revmap_filename and os.path.exists(revmap_filename):
    revmap = dict(l.strip().split(' ') for l in open(revmap_filename, 'r'))
  else:
    revmap={}

  print 'Filtering...'
  progress = 0
  for rev in revlist:
    progress += 1

    if rev in revmap:
      # If this commit was already processed (with an input revmap), skip
      continue

    if progress % 100 == 0:
      print ' [%d/%d]\r' % (progress, len(revlist)),
      sys.stdout.flush()

    oldcommit = fm.get_commit(rev)
    commit = oldcommit.copy()

    oldparents = commit.parents
    commit.parents = [revmap.get(p, p) for p in commit.parents]

    updatefunc = None

    if msg_filter is not None:
      commit.msg = msg_filter(commit.msg)
    if gtt is not None:
      commit.treehash = gtt.transform(commit.treehash)

    if commit_filter is not None:
      result = commit_filter(fm, rev, commit, oldparents)
      if isinstance(result, str):
        # Special case: string result
        if result != rev:
          revmap[rev] = result
        continue
      elif isinstance(result, tuple):
        commit, updatefunc = result
      else:
        commit = result

    if commit != oldcommit:
      newhash = fm.write_commit(commit)
      revmap[rev] = newhash
      if updatefunc is not None:
        updatefunc(newhash)

  update_refs(fm, reflist, revmap, backup_prefix, tag_filter, msg_filter)

  if revmap_filename:
    revmap_out = open(revmap_filename + '.tmp', 'w')
    for oldrev, newrev in revmap.iteritems():
      # Make sure the revs we're writing are real sha1s, not marks
      newrev = fm.get_mark(newrev)
      revmap_out.write('%s %s\n' % (oldrev, newrev))

  if gtt is not None:
    gtt.dump_stats()
  print 'Filtered %d commits, %d were changed.' % (len(revlist), len(revmap))

  if not filter_manager:
    # Don't close if we were passed one on input
    fm.close()

  if revmap_filename:
    os.rename(revmap_filename + '.tmp', revmap_filename)
