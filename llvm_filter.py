#!/usr/bin/python
import collections
import ConfigParser
import fast_filter_branch
import os
import re
import subprocess
import sys
from multiprocessing.dummy import Pool as ThreadPool

svnrev_re=re.compile('^llvm-svn=([0-9]*)\n', re.MULTILINE)

class CvsFixup(object):
  def __init__(self, fm, tree):
    self.treeref = fast_filter_branch.TreeEntry('40000', tree)
    self.fm = fm

  def cp(self, oldname, newname):
    oldpath = oldname.split('/')
    newpath = newname.split('/')

    old_entry = self.treeref.get_path(self.fm, oldpath)
    if old_entry is None:
      self.treeref = self.treeref.remove_path(self.fm, newpath)
    else:
      self.treeref = self.treeref.add_path(self.fm, newpath, old_entry)

  def mv(self, oldname, newname):
    oldpath = oldname.split('/')
    newpath = newname.split('/')

    old_entry = self.treeref.get_path(self.fm, oldpath)
    if old_entry is None:
      self.treeref = self.treeref.remove_path(self.fm, newpath)
    else:
      self.treeref = self.treeref.remove_path(self.fm, oldpath)
      self.treeref = self.treeref.add_path(self.fm, newpath, old_entry)

  def rm(self, name):
    path = name.split('/')
    self.treeref = self.treeref.remove_path(self.fm, path)

  def addfile(self, name, githash, exe=False):
    path = name.split('/')
    if exe:
      mode='100755'
    else:
      mode = '100644'
    self.treeref = self.treeref.add_path(self.fm, path, fast_filter_branch.TreeEntry(mode, githash))

  def finalize(self):
    self.treeref.write_subentries(self.fm)
    return self.treeref.githash

class Filterer(object):
  additional_commit_merges = [
      # Release 3.2 -- all these should merge into 167704.
      167705, 167706, 167707, 167708, 167709, 167710, 167711, 167712, 167713,
      # Release 2.9:
      127212,
      # Release 2.8:
      113053
  ]

  # This is the list of branches that existed at the point CVS still
  # was in use.
  cvs_branch_names = [
      'refs/heads/llvm',
      'refs/heads/llvm-nightlytester',
      'refs/heads/parallel',
      'refs/heads/poolalloc',
      'refs/heads/PowerPC_0',
      'refs/heads/release_1',
      'refs/heads/release_11',
      'refs/heads/release_12',
      'refs/heads/release_13',
      'refs/heads/release_14',
      'refs/heads/release_15',
      'refs/heads/release_16',
      'refs/heads/release_17',
      'refs/heads/release_18',
      'refs/heads/release_19',
      'refs/heads/release_20',
      'refs/heads/SVA',
      'refs/heads/vector_llvm',
      'refs/heads/svntag/comeback',
      'refs/heads/svntag/initial-checkin',
      'refs/heads/svntag/intel_release',
      'refs/heads/svntag/JTC_POOL_WORKS',
      'refs/heads/svntag/main_lastmerge',
      'refs/heads/svntag/May2007',
      'refs/heads/svntag/OldStatistics',
      'refs/heads/svntag/PA_111',
      'refs/heads/svntag/PA_112',
      'refs/heads/svntag/pldi2005',
      'refs/heads/svntag/PowerPC_0_0',
      'refs/heads/svntag/rel19_lastmerge',
      'refs/heads/svntag/RELEASE_1',
      'refs/heads/svntag/RELEASE_11',
      'refs/heads/svntag/RELEASE_12',
      'refs/heads/svntag/RELEASE_13',
      'refs/heads/svntag/RELEASE_14',
      'refs/heads/svntag/RELEASE_15',
      'refs/heads/svntag/RELEASE_16',
      'refs/heads/svntag/RELEASE_19',
      'refs/heads/svntag/RELEASE_20',
      'refs/heads/svntag/start',
      ]

  def __init__(self, repo_name, authors_filename):
    self.repo_name = repo_name
    self.authormap = self.read_authormap(authors_filename)
    self.cvs_branchpoints = None
    self.pool = ThreadPool()

  def read_authormap(self, authors_filename):
    authormap=collections.defaultdict(list)
    cfg = ConfigParser.RawConfigParser()
    cfg.read(authors_filename)
    for svnauthor,email in cfg.items('authors'):
      if '@' in svnauthor:
        svnauthor,before_rev=svnauthor.split('@',1)
        before_rev=int(before_rev)
      else:
        before_rev = 2**64
      authormap[svnauthor].append((before_rev,email))

    for l in authormap.itervalues():
      l.sort()

    return authormap

  def update_cvs_trunk_rev_map(self):
    # Make a mapping from branch commit hash to the trunk commit it
    # was related to.  This is used in the CVS fixups, as those are
    # encoded by trunk version number.
    cvs_branchpoints = {}
    def get_branchdata(branch):
      p = subprocess.Popen(['git', 'merge-base', 'refs/heads/master', branch], stdout=subprocess.PIPE)
      output, unused_err = p.communicate()
      retcode = p.poll()
      if retcode:
        return
      base_rev = output.strip()
      for rev in subprocess.Popen(['git', 'rev-list', branch, '^refs/heads/master'], stdout=subprocess.PIPE).stdout:
        cvs_branchpoints[rev.strip()] = (branch, base_rev)

    self.pool.map(get_branchdata, self.cvs_branch_names)
    self.cvs_branchpoints = cvs_branchpoints

  def get_branch_and_trunk_commit(self, githash):
    if self.cvs_branchpoints is None:
      self.update_cvs_trunk_rev_map()
    return self.cvs_branchpoints.get(githash, (None, None))

  def fixup_cvs_file_moves(self, fm, githash, commit, svnrev):
    # Prior to LLVM's conversion to SVN, the version control history
    # is a disaster. Because CVS had no native move/copy support, many
    # people would go into the repository and move the *,v files
    # around manually. This, needless to say, totally messes up the
    # history.
    #
    # Therefore, the current SVN repository is in various states of
    # disarray for all release tags/branches prior to release_21.
    #
    # Much of the prior history (from r3583 on) is reconstructable via
    # the llvm-commits mailing list messages, which list the files as
    # they were named at the time they were modified.
    #
    # r37801 was the first commit that was actually made in SVN; we
    # don't need to look past there.
    #
    # ...well...there were two more CVS imports later on, too...
    # r38537 to r38420 imported clang (but no branches, phew!)
    # r87107 to r88660 imported safecode (with lots of branches, but
    # it's excluded from monorepo)

    if not svnrev or svnrev >= 37801:
      return commit

    branchname, trunkgithash = self.get_branch_and_trunk_commit(githash)
    if trunkgithash:
      trunkcommit = fm.get_commit(trunkgithash)
      trunkrev = self.find_svnrev(self.msg_filter(trunkcommit.msg))
    else:
      trunkrev = svnrev

    c = CvsFixup(fm, commit.tree)
    if self.repo_name == "monorepo":
      self.fixup_cvs_file_moves_monorepo(trunkrev, c)
    commit.tree = c.finalize()
    return commit

  def fixup_cvs_file_moves_monorepo(self, trunkrev, c):
    if trunkrev < 37632:
      # Don't know when files moved, but r37632 fixed DebugFilename.c to work with its new name
      c.mv('llvm/test/CFrontend/2004-02-13-Memset.c', 'llvm/test/CFrontend/2004-02-13-Memset.c.tr')
      c.mv('llvm/test/CFrontend/2004-02-14-ZeroInitializer.c', 'llvm/test/CFrontend/2004-02-14-ZeroInitializer.c.tr')
      c.mv('llvm/test/CFrontend/2006-09-25-DebugFilename.c', 'llvm/test/CFrontend/2006-09-25-DebugFilename.c.tr')

    if trunkrev < 36886:
      # lib/Bytecode/Archive moved to lib/Archive; ,v copied
      c.rm('llvm/lib/Archive')

    if trunkrev >= 15825 and trunkrev < 34761:
      # Moved away from lib/Support at first rev, then moved back at second (where the ,v was copied, overwriting the orignal)
      c.rm("llvm/lib/Support/ConstantRange.cpp")

    if trunkrev < 34653:
      # ConstantFolding renamed to ConstantFold; .cpp ,v renamed, while .h,v copied.
      c.mv('llvm/lib/VMCore/ConstantFold.cpp', 'llvm/lib/VMCore/ConstantFolding.cpp')
      c.rm('llvm/lib/VMCore/ConstantFold.h')

    if trunkrev < 34064:
      # CStringMap.{cpp,h} renamed to StringMap.{cpp,h}; ,v copied
      c.rm('llvm/include/llvm/ADT/StringMap.h')
      c.rm('llvm/lib/Support/StringMap.cpp')

    if trunkrev < 33748:
      # DenseMap.h renamed to IndexedMap.h; ,v copied
      c.rm('llvm/include/llvm/ADT/IndexedMap.h')

    if trunkrev < 33296:
      # At around r33296, the ,v files in test/Regression/ were moved up one level.
      # Except .cvsignore, which was deleted. (same content as in Analysis tho)
      c.cp('llvm/test/Analysis/.cvsignore', 'llvm/test/Regression/.cvsignore')
      for x in ('Analysis', 'Archive', 'Assembler', 'BugPoint', 'Bytecode',
                'CFrontend', 'C++Frontend', 'CodeGen', 'Debugger',
                'ExecutionEngine', 'LLC', 'Linker', 'Other', 'TableGen',
                'Transforms', 'Verifier'):
        c.mv('llvm/test/'+x, 'llvm/test/Regression/'+x)

    if trunkrev < 33278:
      # Copied from llvm/projects/Stacker to stacker/; ,v copied
      c.rm('stacker')

#    if trunkrev < 32575 and branchname != 'refs/heads/release_19':
#      c.rm('FIXMEFIXMEpoolalloc?')

    if trunkrev < 30864:
      # moved at some point...between 30591 and 30864
      c.mv('llvm/lib/Target/Alpha/README.txt', 'llvm/lib/Target/Alpha/Readme.txt')

    if trunkrev < 29762:
      c.rm('llvm/tools/opt/AnalysisWrappers.cpp')
      c.rm('llvm/tools/opt/GraphPrinters.cpp')
      c.rm('llvm/tools/opt/PrintSCC.cpp')

    if trunkrev < 29716:
      # llvm.spec renamed to llvm.spec.in; ,v copied.
      c.rm('llvm/llvm.spec.in')

    if trunkrev < 29324:
      # ,v moved; exact revision unknown, but around here.
      c.mv('llvm/lib/CodeGen/SelectionDAG/TargetLowering.cpp', 'llvm/lib/Target/TargetLowering.cpp')
      c.mv('llvm/lib/Transforms/Utils/LowerAllocations.cpp', 'llvm/lib/Transforms/Scalar/LowerAllocations.cpp')
      c.mv('llvm/lib/Transforms/Utils/LowerInvoke.cpp', 'llvm/lib/Transforms/Scalar/LowerInvoke.cpp')
      c.mv('llvm/lib/Transforms/Utils/LowerSelect.cpp', 'llvm/lib/Transforms/Scalar/LowerSelect.cpp')
      c.mv('llvm/lib/Transforms/Utils/LowerSwitch.cpp', 'llvm/lib/Transforms/Scalar/LowerSwitch.cpp')
      c.mv('llvm/lib/Transforms/Utils/Mem2Reg.cpp', 'llvm/lib/Transforms/Scalar/Mem2Reg.cpp')
      c.mv('llvm/lib/VMCore/ValueTypes.cpp', 'llvm/lib/CodeGen/ValueTypes.cpp')

    if trunkrev < 28699:
      # ToolRunner moved to bugpoint; ,v copied
      c.rm('llvm/tools/bugpoint/ToolRunner.cpp')
      c.rm('llvm/tools/bugpoint/ToolRunner.h')

    if trunkrev < 27913:
      # Moved from llvm/utils/llvm-config; ,v copied.
      c.rm('llvm/tools/llvm-config')

    if trunkrev < 27468:
      # llvm/lib/VMCore/ConstantRange.cpp moved; ,v copied
      c.rm('llvm/lib/Analysis/ConstantRange.cpp')

    if trunkrev < 25985:
      # Renamed SparcV8 target to Sparc; copied ,v files.
      c.rm('llvm/lib/Target/Sparc')

    if trunkrev < 23998:
      # Moved sometime around here
      c.mv('llvm/lib/Transforms/Utils/LoopSimplify.cpp', 'llvm/lib/Transforms/Scalar/LoopSimplify.cpp')

    if trunkrev < 23918:
      c.rm('llvm/tools/analyze/PrintSCC.cpp')

    if trunkrev < 23745:
      c.mv('llvm/lib/Target/PowerPC/PPCInstrInfo.h', 'llvm/lib/Target/PowerPC/PPC32InstrInfo.h')
      c.mv('llvm/lib/Target/PowerPC/PPCJITInfo.h', 'llvm/lib/Target/PowerPC/PowerPCJITInfo.h')
      c.mv('llvm/lib/Target/PowerPC/PPCRegisterInfo.h', 'llvm/lib/Target/PowerPC/PPC32RegisterInfo.h')
      c.mv('llvm/lib/Target/PowerPC/PPCRelocations.h', 'llvm/lib/Target/PowerPC/PPC32Relocations.h')
      c.mv('llvm/lib/Target/PowerPC/PPCTargetMachine.h', 'llvm/lib/Target/PowerPC/PPC32TargetMachine.h')
      c.mv('llvm/lib/Target/PowerPC/PPCCodeEmitter.cpp', 'llvm/lib/Target/PowerPC/PPC32CodeEmitter.cpp')
      c.mv('llvm/lib/Target/PowerPC/PPCISelPattern.cpp', 'llvm/lib/Target/PowerPC/PPC32ISelPattern.cpp')
      c.mv('llvm/lib/Target/PowerPC/PPCInstrInfo.cpp', 'llvm/lib/Target/PowerPC/PPC32InstrInfo.cpp')
      c.mv('llvm/lib/Target/PowerPC/PPCJITInfo.cpp', 'llvm/lib/Target/PowerPC/PPC32JITInfo.cpp')
      c.mv('llvm/lib/Target/PowerPC/PPCRegisterInfo.cpp', 'llvm/lib/Target/PowerPC/PPC32RegisterInfo.cpp')

    if trunkrev < 23743:
      c.mv('llvm/lib/Target/PowerPC/PPC.h', 'llvm/lib/Target/PowerPC/PowerPC.h')
      c.mv('llvm/lib/Target/PowerPC/PPCFrameInfo.h', 'llvm/lib/Target/PowerPC/PowerPCFrameInfo.h')
      c.mv('llvm/lib/Target/PowerPC/PPCAsmPrinter.cpp', 'llvm/lib/Target/PowerPC/PowerPCAsmPrinter.cpp')
      c.mv('llvm/lib/Target/PowerPC/PPCBranchSelector.cpp', 'llvm/lib/Target/PowerPC/PowerPCBranchSelector.cpp')
      c.mv('llvm/lib/Target/PowerPC/PPCTargetMachine.cpp', 'llvm/lib/Target/PowerPC/PowerPCTargetMachine.cpp')

    if trunkrev < 23742:
      c.mv('llvm/lib/Target/PowerPC/PPCInstrBuilder.h', 'llvm/lib/Target/PowerPC/PowerPCInstrBuilder.h')

    if trunkrev < 23740:
      c.mv('llvm/lib/Target/PowerPC/PPCInstrFormats.td', 'llvm/lib/Target/PowerPC/PowerPCInstrFormats.td')
      c.mv('llvm/lib/Target/PowerPC/PPCInstrInfo.td', 'llvm/lib/Target/PowerPC/PowerPCInstrInfo.td')
      c.mv('llvm/lib/Target/PowerPC/PPCRegisterInfo.td', 'llvm/lib/Target/PowerPC/PowerPCRegisterInfo.td')

    if trunkrev < 23400:
      c.rm('llvm/include/llvm/CodeGen/LiveInterval.h')
      c.rm('llvm/include/llvm/CodeGen/LiveIntervalAnalysis.h')

    if trunkrev < 22900:
      # Don't know when these moved; chose an arbitraryish revision.
      c.mv('llvm/test/Regression/CodeGen/X86/2004-04-09-SameValueCoalescing.llx', 'llvm/test/Regression/CodeGen/Generic/2004-04-09-SameValueCoalescing.llx')
      c.mv('llvm/test/Regression/CodeGen/X86/shift-folding.ll', 'llvm/test/Regression/CodeGen/Generic/shift-folding.ll')

    if trunkrev < 22404:
      c.rm('llvm/include/llvm/Support/MutexGuard.h')

    if trunkrev < 21501:
      c.rm('llvm/docs/CommandGuide/llvm-extract.pod')

    if trunkrev < 21498:
      c.rm('llvm/tools/llvm-extract/llvm-extract.cpp')

    if trunkrev < 19426:
      # Renamed .cpp files to .inc
      c.rm('llvm/lib/System/Unix/MappedFile.inc')
      c.rm('llvm/lib/System/Unix/Memory.inc')
      c.rm('llvm/lib/System/Unix/Path.inc')
      c.rm('llvm/lib/System/Unix/Process.inc')
      c.rm('llvm/lib/System/Unix/Program.inc')
      c.rm('llvm/lib/System/Unix/Signals.inc')
      c.rm('llvm/lib/System/Unix/TimeValue.inc')
      c.rm('llvm/lib/System/Win32/DynamicLibrary.inc')
      c.rm('llvm/lib/System/Win32/MappedFile.inc')
      c.rm('llvm/lib/System/Win32/Memory.inc')
      c.rm('llvm/lib/System/Win32/Path.inc')
      c.rm('llvm/lib/System/Win32/Process.inc')
      c.rm('llvm/lib/System/Win32/Program.inc')
      c.rm('llvm/lib/System/Win32/Signals.inc')
      c.rm('llvm/lib/System/Win32/TimeValue.inc')

    if trunkrev < 17743:
      c.rm('llvm/include/llvm/Linker.h')

    if trunkrev < 17538:
      for x in ['2002-04-14-UnexpectedUnsignedType.ll',
                '2002-04-16-StackFrameSizeAlignment.ll',
                '2003-05-27-phifcmpd.ll',
                '2003-05-27-useboolinotherbb.ll',
                '2003-05-27-usefsubasbool.ll',
                '2003-05-28-ManyArgs.ll',
                '2003-05-30-BadFoldGEP.ll',
                '2003-05-30-BadPreselectPhi.ll',
                '2003-07-06-BadIntCmp.ll',
                '2003-07-07-BadLongConst.ll',
                '2003-07-08-BadCastToBool.ll',
                '2003-07-29-BadConstSbyte.ll',
                'BurgBadRegAlloc.ll',
                'badCallArgLRLLVM.ll',
                'badFoldGEP.ll',
                'badarg6.ll',
                'badlive.ll',
                'constindices.ll',
                'fwdtwice.ll',
                'negintconst.ll',
                'sched.ll',
                'select.ll',
                'spillccr.ll']:
        c.rm('llvm/test/Regression/CodeGen/Generic/'+x)

    if trunkrev < 17380:
      c.rm('llvm/docs/UsingLibraries.html')

    if trunkrev < 16849:
      # Moved from lib/CodeGen/InstrSched
      c.rm('llvm/lib/Target/SparcV9/InstrSched')

    if trunkrev < 16137:
      # Moved from include/Support and include/Config to these
      # locations; ,v copied.
      c.rm('llvm/include/llvm/ADT')
      c.rm('llvm/include/llvm/Config')
      c.rm('llvm/include/llvm/Support/MallocAllocator.h')
      c.rm('llvm/include/llvm/Support/MathExtras.h')
      c.rm('llvm/include/llvm/Support/ThreadSupport.h.in')
      c.rm('llvm/include/llvm/Support/Tree.h')
      c.rm('llvm/include/llvm/Support/ilist')
      c.rm('llvm/include/llvm/Support/EquivalenceClasses.h')
      c.rm('llvm/include/llvm/Support/GraphWriter.h')
      c.rm('llvm/include/llvm/Support/SetVector.h')
      c.rm('llvm/include/llvm/Support/ThreadSupport-NoSupport.h')
      c.rm('llvm/include/llvm/Support/hash_map.in')
      c.rm('llvm/include/llvm/Support/FileUtilities.h')
      c.rm('llvm/include/llvm/Support/SlowOperationInformer.h')
      c.rm('llvm/include/llvm/Support/SystemUtils.h')
      c.rm('llvm/include/llvm/Support/DynamicLinker.h')
      c.rm('llvm/include/llvm/Support/LeakDetector.h')
      c.rm('llvm/include/llvm/Support/CommandLine.h')
      c.rm('llvm/include/llvm/Support/TypeInfo.h')
      c.rm('llvm/include/llvm/Support/Annotation.h')
      c.rm('llvm/include/llvm/Support/Timer.h')
      c.rm('llvm/include/llvm/Support/StringExtras.h')
      c.rm('llvm/include/llvm/Support/BitSetVector.h')
      c.rm('llvm/include/llvm/Support/PostOrderIterator.h')
      c.rm('llvm/include/llvm/Support/DOTGraphTraits.h')
      c.rm('llvm/include/llvm/Support/Casting.h')
      c.rm('llvm/include/llvm/Support/VectorExtras.h')
      c.rm('llvm/include/llvm/Support/ThreadSupport-PThreads.h')
      c.rm('llvm/include/llvm/Support/Statistic.h')
      c.rm('llvm/include/llvm/Support/ELF.h')
      c.rm('llvm/include/llvm/Support/Debug.h')
      c.rm('llvm/include/llvm/Support/STLExtras.h')
      c.rm('llvm/include/llvm/Support/iterator.in')
      c.rm('llvm/include/llvm/Support/GraphTraits.h')
      c.rm('llvm/include/llvm/Support/DenseMap.h')
      c.rm('llvm/include/llvm/Support/.cvsignore')
      c.rm('llvm/include/llvm/Support/HashExtras.h')
      c.rm('llvm/include/llvm/Support/PluginLoader.h')
      c.rm('llvm/include/llvm/Support/DepthFirstIterator.h')
      c.rm('llvm/include/llvm/Support/SCCIterator.h')
      c.rm('llvm/include/llvm/Support/DataTypes.h.in')
      c.rm('llvm/include/llvm/Support/type_traits.h')
      c.rm('llvm/include/llvm/Support/SetOperations.h')
      c.rm('llvm/include/llvm/Support/hash_set.in')

    if trunkrev < 16003:
      # Moved from llvm/examples/ModuleMaker/tools/ModuleMaker/ModuleMaker.cpp')
      c.rm('llvm/examples/ModuleMaker/ModuleMaker.cpp')

    if trunkrev < 16002:
      # Moved from llvm/projects/SmallExamples/
      c.rm('llvm/examples/Fibonacci')
      c.rm('llvm/examples/ModuleMaker')
      c.rm('llvm/examples/HowToUseJIT')

    if trunkrev < 16001:
      c.rm('llvm/examples/Makefile')

    if trunkrev < 15925:
      # Moved from llvm/projects/.. into SmallExamples
      c.rm('llvm/projects/SmallExamples/ModuleMaker')
      c.rm('llvm/projects/SmallExamples/HowToUseJIT')

    if trunkrev < 11826:
      # Renamed Sparc to SparcV9; copied ,v files (and the originals then got deleted later on...)
      c.mv('llvm/lib/Target/SparcV9', 'llvm/lib/Target/Sparc')

    # TODO: there's more cleanups that could be done earlier.

    if trunkrev < 7750:
      c.rm('poolalloc')

    # Some binary files were not marked binary, and had their CR bytes
    # mangled into LFs. Restore the originals from release tarballs
    # where possible.
    # FIXME: only add when it existed.
    if trunkrev < 32132:
      if trunkrev >= 26473:
        c.addfile('llvm/test/Regression/Bytecode/memcpy.ll.bc-16', '12a322e5e3f9b9d8bc6021435faffb754fcfb91c')
      if trunkrev >= 25442:
        c.addfile('llvm/test/Regression/Bytecode/old-intrinsics.ll.bc-16', '228757e5771bd3af1ded150832a35385ae49c559', exe=True)
      if trunkrev >= 25681:
        c.addfile('llvm/test/Regression/Bytecode/signed-intrinsics.ll.bc-16', '75cf643e748e889d7f46a438c5ce32bda02c2b74')

    if trunkrev >= 15921 and trunkrev < 31723:
      c.addfile('llvm/test/Regression/Bytecode/slow.ll.bc-13', 'f9a6406b6ea4c931904b0599f4f2efe020721b99')

    if trunkrev >= 29646 and trunkrev < 32143:
      # This file has more revisions which are probably broken, but
      # there's no available copy of them.
      c.addfile('llvm/test/Regression/Transforms/LoopSimplify/2006-08-11-LoopSimplifyLongTime.ll.bc', '9ccf0117c09fa458479a32efef0b46c41dbe398d')

  def msg_filter(self, msg):
    # Clean up svn2git cruft in commit messages.  Also deal with
    # extraneous trailing newlines, and add a note where there's no
    # commit message other than the added revision info.
    msg = re.sub('\n+svn path=[^\n]*; revision=([0-9]*)\n?$', '\n\nllvm-svn=\\1\n', msg)
    if msg.startswith("\n\nllvm-svn="):
      msg = '(no commit message)' + msg
    return msg

  def combine_consecutive_merges(self, fm, commit, svnrev):
    # Multiple commits are used to make each branch -- one for each
    # subproject -- so the branch creation is uglier than necessary
    # (deleting a bunch of files during the initial branch, then
    # re-adding them in the followup merge-commits). We want to
    # collapse those commits into one.

    # This detects a merge commit with the same message, author, and
    # other-parents as its first-parent.
    def msg_ignoring_svnrev(msg):
      return svnrev_re.sub('', msg)

    if len(commit.parents) > 1:
      parent = fm.get_commit(commit.parents[0])
      if (commit.author == parent.author and
          commit.committer == parent.committer and
          (svnrev in self.additional_commit_merges or
           msg_ignoring_svnrev(commit.msg) == msg_ignoring_svnrev(parent.msg)) and
          (commit.parents[1:] == parent.parents or
           commit.parents[1:] == parent.parents[1:])):
        # The parent commit looks similar, so merge it.  (preserve the
        # revision number from its commit message, though).
        commit.msg += ''.join(m.group(0) for m in svnrev_re.finditer(parent.msg))
        commit.parents = parent.parents[:]

    return commit

  def get_new_author(self, svnrev, oldauthor):
    if oldauthor == 'SVN to Git Conversion <nobody@llvm.org>':
      return oldauthor

    # Extract only the name, ignore the email.
    oldauthor = oldauthor.split(' <')[0]
    for entry in self.authormap[oldauthor.lower()]:
      if svnrev < entry[0]:
        return entry[1]
    raise Exception("Can't find author mapping for %s at %d" % (oldauthor, svnrev))

  def author_fixup(self, fm, commit, svnrev):
    commit.author = self.get_new_author(svnrev, commit.author)
    commit.committer = self.get_new_author(svnrev, commit.committer)
    return commit

  def find_svnrev(self, msg):
    re_match = svnrev_re.search(msg)
    if not re_match:
      raise Exception("Can't find svn revision in %r", msg)
    return int(re_match.group(1))

  def commit_filter(self, fm, githash, commit):
    try:
      svnrev = self.find_svnrev(commit.msg)
    except:
      if commit.author == 'SVN to Git Conversion <nobody@llvm.org>':
        parent = fm.get_commit(commit.parents[0])
        svnrev = self.find_svnrev(parent.msg)
      else:
        raise

    commit = self.fixup_cvs_file_moves(fm, githash, commit, svnrev)
    commit = self.author_fixup(fm, commit, svnrev)
    commit = self.combine_consecutive_merges(fm, commit, svnrev)
    return commit

  def run(self):
    if self.repo_name == "monorepo":
      file_changes = [
          # At one point a zip file of all of llvm was checked into
          # lldb. This is quite large, so we want to delete it.
          ('/lldb/llvm.zip', lambda fm, path, githash: None),
      ]
    elif self.repo_name == "www":
      # TODO: remove after next repo rebuild.
      file_changes = [
          ('/devmtg/2013-04/krzikalla-lores.mov', lambda *args: None),
          ('/devmtg/2013-04/pellegrini-lores.mov', lambda *args: None),
          ('/devmtg/2013-04/jasper-lores.mov', lambda *args: None),
          ('/devmtg/2013-04/stepanov-lores.mov', lambda *args: None),
      ]
    else:
      file_changes = []

    fast_filter_branch.do_filter(global_file_actions=file_changes,
                                 msg_filter=self.msg_filter,
                                 commit_filter=self.commit_filter,
                                 backup_prefix=None,
                                 revmap_filename="llvm_filter.revmap")

if __name__=="__main__":
  Filterer(sys.argv[1], sys.argv[2]).run()
