#!/usr/bin/python

# Merges in new entries to the author-ids.conf from the
# svn-mailer.conf file from the svn host. New entries are copied in,
# and changed emails/names cause the old info to be given an "@1234"
# suffix, which indicates the maximum revision number that data is
# used for.

import ConfigParser
import os
import sys
import tempfile

def read_authormap(filename):
  cfg = ConfigParser.RawConfigParser()
  cfg.read(filename)

  d = {}
  for author, email in cfg.items('authors'):
    d[author] = email

  return d

def update_from_svn_mailer(authorids_filename, svn_mailer_filename, svnrev):
  authormap = read_authormap(authorids_filename)
  updates = read_authormap(svn_mailer_filename)
  for author, new_email in updates.iteritems():
    assert '@' not in author

    old_email = authormap.get(author)
    if old_email != new_email:
      # Email/realname added or changed
      if old_email is not None:
        # Move existing entry to name@svnrev key.
        assert "%s@%s" % (author, svnrev) not in authormap
        authormap["%s@%s" % (author, svnrev)] = old_email
      authormap[author] = new_email

  return authormap

def print_authors(authormap, output_filename):
  with tempfile.NamedTemporaryFile(
      dir=os.path.dirname(output_filename), delete=False) as fout:
    fout.write("[authors]\n")

    for author, email in sorted(authormap.iteritems()):
      fout.write("%s = %s\n" % (author, email))

  os.rename(fout.name, output_filename)

def main():
  if len(sys.argv) < 3:
    sys.stderr.write("Usage: update-authormap.py author-ids.conf svnmailer-file cur-svn-revision\n")
  authormap_filename = sys.argv[1]
  svnmailer_conf_filename = sys.argv[2]
  svn_rev = sys.argv[3]

  new_authors = update_from_svn_mailer(authormap_filename, svnmailer_conf_filename, svn_rev)
  print_authors(new_authors, sys.argv[1])

if __name__ == "__main__":
  main()
