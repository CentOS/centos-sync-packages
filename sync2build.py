#! /usr/bin/python3

"""Sync packages from a dist-git repository to a koji build system.
Eg. Build packages which are newer in git than in the tag/compose we are
looking at.
"""

from __future__ import print_function

import koji
import json
import sys
import os
import shutil
import tempfile
import time
from optparse import OptionParser

import spkg
import matchlist

# Keep builds around for 4 hours
conf_ttl_builds = 60*60*4

# Keep failed builds around for 18 hours (doesn't rebuild during that time)
conf_ttl_failed_builds = 60*60*18

# Do we want to filter through the CVE checker
conf_filter_cve = True

# Just do the downloads, and don't alt-src
conf_data_downloadonly = False

# Create temp. dirs. for alt-src.
conf_alt_src_tmp = True

# Cache looking up tags for builds.
conf_cache_builds = False

# Do we want to output old tags data, useful for debugging
__output_old_tags = False

# Do we want to output build data, useful for debugging
__output_build_lines = False

# Do we want to include all packages from a compose...
__auto_compose_allowlist = True

# Do we want to include all packages from a tag...
__auto_tag_allowlist = True

# Do we want to just test...
__test_print_tagged = False

if not __test_print_tagged:
    import git

if not hasattr(tempfile, 'TemporaryDirectory'):
    class TemporaryDirectory(object):
        """Do it using __del__ as a hack """

        def __init__(self, suffix='', prefix='tmp', dir=None):
            self.name = tempfile.mkdtemp(suffix, prefix, dir)

        def __del__(self):
            shutil.rmtree(self.name)
    tempfile.TemporaryDirectory = TemporaryDirectory

# This is mostly hacked from: koji_cli/commands.py buildinfo/rpminfo
def koji_nvr2srpm(session, nvr):
    """ Given an rpm nvr, convert it into an srpm nvr for CVE checker.
        Also takes a build_id, due to API leakage.
    """
    buildinfo = session.getBuild(nvr)
    if buildinfo is None:
        return None

    buildinfo['name'] = buildinfo['package_name']
    buildinfo['arch'] = 'src'
    epoch = buildinfo['epoch']
    if buildinfo['epoch'] is None:
        buildinfo['epoch'] = ""
        epoch = '0'
    else:
        buildinfo['epoch'] = str(buildinfo['epoch']) + ":"

    snvr = buildinfo['name']
    snvr += '-'
    snvr += buildinfo['version']
    snvr += '-'
    snvr += buildinfo['release']
    ent = {'package_name' : buildinfo['name'], 'nvr' : snvr,
           # These aren't used atm.
           'name' : buildinfo['name'], 'version' : buildinfo['version'],
           'release' : buildinfo['release'],
           'epoch' : None}
    return ent

# This is mostly copied and pasted from: koji_cli/commands.py rpminfo
def koji_nvra2srpm(session, nvra):
    """ Given an rpm nvra, convert it into an srpm nvr for CVE checker.
    """
    info = session.getRPM(nvra)
    if info is None:
        return None

    if info['epoch'] is None:
        info['epoch'] = ""
    else:
        info['epoch'] = str(info['epoch']) + ":"

    if info.get('external_repo_id'):
        repo = session.getExternalRepo(info['external_repo_id'])
        print("External Repository: %(name)s [%(id)i]" % repo)
        print("External Repository url: %(url)s" % repo)
        return None

    return koji_nvr2srpm(session, info['build_id'])

ml_pkgs = matchlist.Matchlist()
def load_package_list():
    ml_pkgs.load("conf/sync2build-packages.txt")

ml_pkgdeny = matchlist.Matchlist()
ml_gitdeny = matchlist.Matchlist()
def load_package_denylist():
    ml_gitdeny.load("conf/sync2build-gittags-denylist.txt")
    ml_pkgdeny.load("conf/sync2build-packages-denylist.txt")

_koji_max_query = 2000
def koji_archpkgs2sigs(kapi, pkgs):
    if len(pkgs) > _koji_max_query:
        for i in range(0, len(pkgs), _koji_max_query):  
            koji_archpkgs2sigs(kapi, pkgs[i:i + _koji_max_query])
        return

    # Get unsigned packages
    kapi.multicall = True
    # Query for the specific key we're looking for, no results means
    # that it isn't signed and thus add it to the unsigned list
    for pkg in pkgs:
        kapi.queryRPMSigs(rpm_id=pkg._koji_rpm_id)

    results = kapi.multiCall()
    for ([result], pkg) in zip(results, pkgs):
        pkg.sighash = []
        pkg.signed = []
        for res in result:
            if not res['sigkey']:
                continue
            pkg.sighash.append(res['sighash'])
            pkg.signed.append(res['sigkey'])
        if len(pkg.signed) == 0:
            pkg.sighash = ''
            pkg.signed = ''
        if len(pkg.signed) == 1:
            pkg.sighash = pkg.sighash[0]
            pkg.signed = pkg.signed[0]

def koji_pkgs2archsigs(kapi, pkgs, filter=None):
    if len(pkgs) > _koji_max_query:
        ret = []
        for i in range(0, len(pkgs), _koji_max_query):
            npkgs = pkgs[i:i + _koji_max_query]
            ret.extend(koji_pkgs2archsigs(kapi, npkgs, filter))
        return ret

    if filter is None:
        filter = lambda x: False

    kapi.multicall = True
    for pkg in pkgs:
        kapi.listRPMs(buildID=pkg._koji_build_id)

    ret = []
    results = kapi.multiCall()
    for ([rpms], bpkg) in zip(results, pkgs):
        for rpm in rpms:
            epoch = spkg.epochnum2epoch(rpm['epoch'])
            pkg = spkg.nvr2pkg(rpm['nvr'], arch=rpm['arch'], epoch=epoch)
            pkg._koji_rpm_id = rpm['id']
            pkg._koji_build_id = bpkg._koji_build_id
            if filter(pkg):
                continue
            ret.append(pkg)

    koji_archpkgs2sigs(kapi, ret)
    return ret

def _pkg_koji_task_state(self):
    if not hasattr(self, '_cached_koji_task_state'):
        tinfo = self._kapi.getTaskInfo(self._koji_task_id)
        # This overwrites the property call
        self._cached_koji_task_state = _task_state(tinfo)
        del self._kapi
    return self._cached_koji_task_state
# This is a hack, so we can continue to use spkg.Pkg() indirectly. Sigh.
spkg.Pkg._koji_task_state = property(_pkg_koji_task_state)

def _koji_buildinfo2pkg(kapi, binfo):
    epoch = spkg.epochnum2epoch(binfo['epoch'])
    pkg = spkg.nvr2pkg(binfo['nvr'], epoch=epoch)
    pkg._koji_build_id = binfo['build_id']
    if 'task_id' in binfo:
        pkg._koji_task_id = binfo['task_id']
        pkg._kapi = kapi
    return pkg

def koji_tag2pkgs(kapi, tag):
    """
    Return a list of latest build packages that are tagged with certain tag
    """
    ret = []
    for rpminfo in kapi.listTagged(tag, inherit=True, latest=True):
        pkg = _koji_buildinfo2pkg(kapi, rpminfo)
        ret.append(pkg)

    return ret

def koji_pkgid2pkgs(kapi, pkgid):
    """
    Return a the build pacakges from a package id
    """
    ret = []
    for binfo in kapi.listBuilds(packageID=pkgid):
        pkg = _koji_buildinfo2pkg(kapi, binfo)
        ret.append(pkg)
    return ret

# Stupid py3...
def b(x):
    return str(x).encode('utf-8')

def koji_tag2checksum(kapi, tag, checksum='sha1'):
    """
    Return a checksum for the packages in a tag
    """

    import hashlib
    pkgs = koji_tag2pkgs(kapi, tag)
    pkgs = koji_pkgs2archsigs(kapi, pkgs)

    num = 0
    ret = hashlib.new(checksum)
    for pkg in sorted(pkgs):
        num += 1
        ret.update(b(pkg.envra))
        ret.update(b(' '))
        ret.update(b(pkg.sighash))
        ret.update(b('\n'))

    return str(num) + ":" + ret.hexdigest()

def koji_tag2srpms_checksum(kapi, tag, checksum='sha1'):
    """
    Return a checksum for the packages in a tag
    """

    import hashlib
    pkgs = koji_tag2pkgs(kapi, tag)
    pkgs = koji_pkgs2archsigs(kapi, pkgs, filter=lambda x: x.arch != 'src')

    num = 0
    ret = hashlib.new(checksum)
    for pkg in sorted(pkgs):
        num += 1
        ret.update(b(pkg.envra))
        ret.update(b(' '))
        ret.update(b(pkg.sighash))
        ret.update(b('\n'))

    return str(num) + ":" + ret.hexdigest()

def composed_url2pkgs(baseurl):
    """
    Return a list of latest packages that are in the given compose
    """
    import compose

    c = compose.Compose(baseurl)
    pdata = c.json_rpms()
    p = compose.packages_from_compose(pdata)
    return p

_cached_upath = None
def _cached_setup():
    if not conf_cache_builds:
        return None

    try:
        import mtimecache
    except:
        return None
    global _cached_upath
    if _cached_upath is None:
        _cached_upath = mtimecache.userappcachedir("sync2build")
        mtimecache.clean_dir(_cached_upath + "nvr")
        mtimecache.clean_dir(_cached_upath + "version-nvr")
    return mtimecache

def cached_nvr(nvr):
    mtimecache = _cached_setup()
    if mtimecache is None:
        return None
    ret = mtimecache.Cache(_cached_upath + "nvr/" + nvr)
    return ret

def cached_version_nvr(version, nvr):
    mtimecache = _cached_setup()
    if mtimecache is None:
        return None
    ret = mtimecache.Cache(_cached_upath + "version-nvr/" + version + '-' + nvr)
    return ret

def check_denylist_builds(bpkgs):
    """
    Look for any builds on the denylist, and remove them.
    """
    ret = []
    for bpkg in sorted(bpkgs):
        if ml_pkgdeny.nvr(bpkg.name, bpkg.version, bpkg.release):
            print("Denied Pkg: ", bpkg)
            sys.stdout.flush()
            continue

        ret.append(bpkg)
    return ret

def build2git_tags(build, codir, T="rpms"):
    giturl = "https://git.centos.org/"
    giturl += T
    giturl += "/"
    giturl += build['package_name']
    giturl += ".git"
    try:
        repo = git.Repo.clone_from(giturl, codir)
        tags = repo.tags
    except git.exc.GitCommandError:
        # This means the clone didn't work, so it's a new package.
        tags = []
    return tags

def bpkg2git_tags(bpkg, codir, T="rpms"):
    giturl = "https://git.centos.org/"
    giturl += T
    giturl += "/"
    giturl += bpkg.name
    giturl += ".git"
    try:
        repo = git.Repo.clone_from(giturl, codir)
        tags = repo.tags
    except git.exc.GitCommandError:
        # This means the clone didn't work, so it's a new package.
        tags = []
    return tags

def _tags2pkgs(tags):
    tpkgs = []
    for tag in tags:
        stag = str(tag)
        if not stag.startswith("imports/c8"):
            continue
        stag = stag[len("imports/c8"):]
        # Eg. See: https://git.centos.org/rpms/ongres-scram/releases
        stag = stag.replace('%7e', '~')
        if '%' in stag: # FIXME? panic?
            continue
        if stag.startswith("s/"):
            stream = True
            stag = stag[len("s/"):]
        elif  stag.startswith("/"):
            stream = False
            stag = stag[len("/"):]
        else:
            continue

        # Tag is now N-V-R
        pkg = spkg.nvr2pkg(stag)
        pkg.stream = stream
        tpkgs.append(pkg)

    return tpkgs

def check_unsynced_builds(bpkgs):
    """
    Look for builds that are not synced with centos streams
    """
    ret = []

    tcoroot = tempfile.TemporaryDirectory(prefix="sync2build-", dir="/tmp")
    corootdir = tcoroot.name + '/'
    print("Using tmp dir:", corootdir)
    for bpkg in sorted(bpkgs):
        if not ml_pkgs.nvr(bpkg.name, bpkg.version, bpkg.release):
            continue

        cb = cached_nvr(bpkg.nvr)
        if cb is not None and cb.cached():
            print("Cached-Tag: ", cb.read())
            continue
        codir = corootdir + bpkg.name

        tags = bpkg2git_tags(bpkg, codir)
        if os.path.exists(codir + '/README.debrand'):
            print("Skip (debranding): ", bpkg)

        tpkgs = _tags2pkgs(tags)
        tpkgs = filter_nonstream_packages(tpkgs)
        tpkgs = filter_el8_branch_packages(tpkgs)
        tpkgs = filter_module_packages(tpkgs)
        tpkgs = filter_rebuild_packages(tpkgs)
        tpkgs = filter_gitdeny_packages(tpkgs)

        for tpkg in sorted(spkg.returnNewestByName(tpkgs)):
            if tpkg.name != bpkg.name:
                print("Err:", tpkg, "!=", pkg)
                continue
            if not tpkg.verGT(bpkg):
                continue
            print("Need to build:", tpkg, "(latest build:", bpkg, ")")
            ret.append(tpkg)

        sys.stdout.flush()
        shutil.rmtree(codir, ignore_errors=True)
    return ret

# Given:
# perl-IO-Tty-1.12-12.module+el8.3.0+6446+37a50855
# We want to match to:
# perl-IO-Tty-1.12-12.module+el8.3.0+6446+594cad75
def nvr2shared_nvr(nvr):
    val = nvr.rfind('+') # Remove the +37a50855 suffix
    if val != -1:
        nvr = nvr[:val]
    if False: # Just remove the last bit?
        return nvr
    val = nvr.rfind('+') # Remove the +6446 suffix
    if val != -1:
        nvr = nvr[:val]
    return nvr

def find_shared_nvr(nvr, builds):
    """
    Given a shared nvr, search through all the build dicts. and see if it
    matches.
    """
    for build in builds:
        snvr = nvr2shared_nvr(build['nvr'])
        if snvr == nvr:
            return True
    return False

def json_nvr2koji_srpm(kapi, rpmnvr):
    ent = koji_nvr2srpm(kapi, rpmnvr)
    if ent is None:
        print("No such koji rpm: %s" % rpmnvr)
    return ent

def _tid2url(tid):
    weburl = "https://koji.mbox.centos.org/koji"
    return "%s/taskinfo?taskID=%d" % (weburl, tid)

def _filter_old_builds(kapi, bpkgs):
    bts = bpids_load(kapi)

    running_builds = {}
    for bt in bts:
        running_builds[bt.pkg.name] = bt

    nbpkgs = []
    for bpkg in sorted(bpkgs):
        if bpkg.name in running_builds:
            bt = running_builds[bpkg.name]
            if bpkg <= bt.pkg:
                print("Skipping rebuild:", bpkg)
                continue
            elif not bt.done:
                print("Already Building:", bpkg)
                print("Task:", _tid2url(bt.tid))
                continue
            nbts = []
            for bt in bts:
                if bt.pkg.name == bpkg.name:
                    continue
                nbts.append(bt)
            bts = nbts

        nbpkgs.append(bpkg)
    return bts, nbpkgs

def build_packages(kapi, bpkgs, tag, giturl='git+https://git.centos.org/rpms/'):
    """
    Build the newer rpms to centos stream tags
    """

    bts, bpkgs = _filter_old_builds(kapi, bpkgs)
    for bpkg in sorted(bpkgs):
        url = giturl + bpkg.name
        if bpkg.stream: # Assume this is always here?
            url += '?#imports/c8s/' + bpkg.nvr
        else:
            url += '?#imports/c8/' + bpkg.nvr
        # print("URL:", url)
        print("Building:", bpkg)
        sys.stdout.flush()

        if conf_data_downloadonly:
            continue

        task_id = kapi.build(url, tag)
        weburl = "https://koji.mbox.centos.org/koji"
        print("Task:", _tid2url(task_id))
        sys.stdout.flush()
        bts.append(BuildTask(kapi, task_id, bpkg))

    return bts

def filter_nonstream_packages(pkgs):
    ret = []
    for pkg in pkgs:
        if not pkg.stream:
            continue
        ret.append(pkg)
    return ret

def filter_el8_branch_packages(pkgs):
    ret = []
    for pkg in pkgs:
        if spkg._is_branch_el8(pkg):
            continue
        ret.append(pkg)
    return ret

def filter_module_packages(pkgs):
    ret = []
    for pkg in pkgs:
        if spkg._is_module(pkg):
            continue
        ret.append(pkg)
    return ret

def filter_rebuild_packages(pkgs):
    ret = []
    for pkg in pkgs:
        if spkg._is_rebuild(pkg):
            continue
        ret.append(pkg)
    return ret

def filter_gitdeny_packages(pkgs):
    ret = []
    for pkg in pkgs:
        if ml_gitdeny.nvr(pkg.name, pkg.version, pkg.release):
            continue
        ret.append(pkg)
    return ret

def sync_packages(tag, compose, kapi):
    """
        tag: Specify a koji tag to pull packages from.
        compose: Specify a "koji" compose to pull packages from (None uses the tag.
        kapi: koji object to query
    """
    if compose is None:
        bpkgs = koji_tag2pkgs(kapi, tag)
        if __auto_tag_allowlist:
            ml_pkgs.all = True
    else:
        bpkgs = composed_url2pkgs(compose)
        if __auto_compose_allowlist:
            ml_pkgs.all = True
    if __test_print_tagged:
        from pprint import pprint
        pprint(bpkgs)
        return
    bpkgs = check_denylist_builds(bpkgs)

     # Quickly check very old task ids. and remove them if done.
    bts = bpids_load(kapi)
    if bts:
        bts, dbts = bpids_wait_packages(kapi, bts, 0)
        for bt in dbts: # keep tasks around for a while.
            if bt.state == 'CLOSED': # Drop successes
                if bt.since > conf_ttl_builds:
                    continue
             # Keep failures around for a bit...
            if bt.since > conf_ttl_failed_builds:
                continue
            bts.append(bt)
        bpids_save(bts)

    bpkgs = check_unsynced_builds(bpkgs)

    bts = build_packages(kapi, bpkgs, tag)
    return bts

def _build_done(state):
    return state != 'BUILDING'
def _build_state(info):
    return koji.BUILD_STATES[info['state']]

def _task_done(state):
    return state in ('CLOSED', 'CANCELED', 'FAILED')
def _task_state(info):
    return koji.TASK_STATES[info['state']]

class BuildTask(object):
    def __init__(self, kapi, tid, pkg):
        self.kapi = kapi

        self.tid = tid
        self.pkg = pkg

        self._info = None

    # Same tid's should have the same pkgs.
    def __eq__(self, o):
        return self.tid == o.tid
    def __lt__(self, o):
        return self.tid < o.tid

    def __str__(self):
        return "Task=%d; Pkg=%s; Status=%s; dur=%s" % (self.tid, self.pkg,
                                                       self.status, self.duration)

    def _get_info(self):
        info = self.kapi.getTaskInfo(self.tid)
        if info is not None:
            self._info = info
            self._tm_info = time.time()
        return self._info

    @property
    def _chk_info(self):
        if self._info is None:
            self._get_info()
        elif _task_done(_task_state(self._info)): # recursion
            pass
        elif time.time()-self._tm_info >= 1: # Don't spam update reqs
            self._get_info()
        return self._info    

    @property
    def done(self):
        # Could also check if self.completion_ts is not None
        return _task_done(self.state)

    @property
    def state(self):
        if self._chk_info is None:
            return None
        return _task_state(self._info)

    @property
    def completion_ts(self):
        if self._chk_info is None:
            return None
        if self._info['completion_ts'] is None:
            return None
        return self._info['completion_ts']

    @property
    def create_ts(self):
        if self._chk_info is None:
            return None
        return self._info['create_ts']

    @property
    def start_ts(self):
        if self._chk_info is None:
            return None
        if self._info['start_ts'] is None:
            return None
        return self._info['start_ts']

    @property
    def duration(self):
        try:
            import mtimecache
        except:
            mtimecache = None
        def fmt_dur(x):
            if mtimecache is None:
                return str(int(x)) + 's'
            return mtimecache.format_duration(x)

        beg = self.start_ts
        if beg is None:
            return '<not-started>'
        end = self.completion_ts
        if end is None:
            dur = fmt_dur(time.time()-beg)
            return dur + '+'
        return fmt_dur(end-beg)

    @property
    def since(self):
        end = self.completion_ts
        if end is None:
            return 0
        now = time.time()
        if now < end:
            return 0
        return now-end

def bpids_wait_packages(kapi, bts, waittm):
    try:
        import mtimecache
    except:
        mtimecache = None

    if not waittm:
        waitsecs = 0
    elif mtimecache is None:
        waitsecs = 2 * 60
    else:
        waitsecs = mtimecache.parse_time(waittm)

    dbts = []

    beg = time.time()
    now = beg
    while bts and (now-beg) <= waitsecs:
        nbts = []
        for bt in sorted(bts):
            if bt.done:
                msg = "Task %s for %s ended on %s (%s): %s"
                tm = time.gmtime(bt.completion_ts)
                tm = time.strftime("%Y-%m-%d %H:%M", tm)
                print(msg % (bt.tid, bt.pkg, tm, bt.duration, bt.state))
                dbts.append(bt)
                continue
            nbts.append(bt)
        bts = nbts
        if bts:
            secs = 20
            if secs+(now-beg) > waitsecs:
                secs = 10
            if secs+(now-beg) > waitsecs:
                secs = 5
            if secs+(now-beg) > waitsecs:
                secs = 1
            if secs+(now-beg) <= waitsecs:
                time.sleep(secs)
        now = time.time()

    return bts, dbts

def bpids_print(bts):
    for bt in sorted(bts):
        dur = bt.duration
        if bt.start_ts is None:
            print("Task %s waiting for %s" % (bt.tid, bt.pkg))
        else:
            print("Task %s running (%s) for %s" % (bt.tid, bt.duration, bt.pkg))


# Stupid format: 
# header = sync2build-bipds-v-1
# # Comments as in normal readfile ... blah.
# <tid> = koji build task id
# pkg-nevra [<space> nevra]*
_bpids_file = "s2b-bpids.data"
_bpids_f_header_v = 'sync2build-bipds-v-1'
def bpids_save(bts, fname=None):
    if fname is None:
        fname = _bpids_file

    if not bts:
        if os.path.exists(fname):
            os.remove(fname)
        return

    iow = open(fname + '.tmp', "w")
    iow.write(_bpids_f_header_v + '\n')
    for bt in sorted(bts):
        iow.write(str(bt.tid) + '\n')
        iow.write(bt.pkg.nevra + '\n')
    iow.close()
    os.rename(fname + '.tmp', fname)

def bpids_load(kapi, fname=None):
    if fname is None:
        fname = _bpids_file

    if not os.path.exists(fname):
        return []

    lines = matchlist.read_lines(fname)
    if not lines:
        print("Bad saved bpids file, empty.")
        sys.exit(8)

    if lines[0] != _bpids_f_header_v:
        print("Bad saved bpids file, no header.")
        sys.exit(8)
    lines.pop(0)

    if len(lines) % 1 != 0:
        print("Bad saved bpids file, odd number of entries.")
        sys.exit(8)

    seen = set()
    bts = []
    while lines:
        tid = lines.pop(0)
        nevra = lines.pop(0)

        tid = int(tid)
        pkg = spkg.nevra2pkg(nevra)

        if tid in seen:
            print("Duplicate task id:", tid)
            continue
        seen.add(tid)

        bts.append(BuildTask(kapi, tid, pkg))
    return bts

def main():
    parser = OptionParser()
    parser.add_option("", "--koji-host", dest="koji_host",
                      help="Host to connect to", default="https://koji.mbox.centos.org/kojihub")
    parser.add_option("", "--packages-tag", dest="packages_tag",
                      help="Specify package tag to sync", default="dist-c8-stream")
    # parser.add_option("", "--modules-tag", dest="modules_tag",
    #                   help="Specify module tag to sync", default="dist-c8-stream-module")
    parser.add_option("", "--packages-compose", dest="packages_compose",
                      help="Specify package compose to sync", default=None)
    # parser.add_option("", "--modules-compose", dest="modules_compose",
    #                   help="Specify module compose to sync", default=None)
    parser.add_option("", "--download-only", dest="download_only",
                      help="Just download, always safe", default=False, action="store_true")
    parser.add_option("", "--nocache", dest="nocache",
                      help="Don't cache any results", default=False, action="store_true")
    parser.add_option("", "--wait", dest="wait",
                      help="Wait time for tasks", default="")


    (options, args) = parser.parse_args()

    kapi = koji.ClientSession(options.koji_host)
    if options.koji_host == "https://koji.mbox.centos.org/kojihub":
        kapi.ssl_login("/compose/.koji/mbox_admin.pem", None, "/compose/.koji/ca.crt")

    load_package_list()
    load_package_denylist()
    
    if options.nocache:
        global conf_cache_builds
        conf_cache_builds = False

    if options.download_only:
        global conf_data_downloadonly
        conf_data_downloadonly = True

    if not args: pass
    elif args[0] in ('list-unsigned-pkgs', 'list-unsigned-packages',
                     'ls-unsigned-pkgs', 'ls-unsigned-packages'):
        args = args[1:]

        tag  = options.packages_tag

        def _slen(x):
            return len(str(len(x)))
        def _out_pkg(prefix, bpkgs):
            bids = set()
            for bpkg in sorted(bpkgs):
                if hasattr(bpkg, 'signed'):
                    if bpkg.signed:
                        continue
                suffix = ''
                bids.add(bpkg._koji_build_id)
                prefix = "%*d | %*d |"
                prefix %= (lenmax, len(bids), 8, bpkg._koji_build_id)
                if hasattr(bpkg, 'stream') and bpkg.stream:
                    suffix += '(stream)'
                if spkg._is_branch_el8(bpkg):
                    suffix += '(branch)'
                if spkg._is_module(bpkg):
                    suffix += '(module)'
                if spkg._is_rebuild(bpkg):
                    suffix += '(rebuild)'
                print(prefix, bpkg, suffix)
        bpkgs = koji_tag2pkgs(kapi, tag)
        bpkgs = koji_pkgs2archsigs(kapi, bpkgs)
        lenmax = _slen(bpkgs) # Max size of printed num
        print("%*s | %*s | pkg" % (lenmax, "bids", 8, "build_id"))
        _out_pkg("Tag:", spkg.match_pkgs(args, bpkgs))
    elif args[0] in ('nvra-unsigned-pkgs', 'nvra-unsigned-packages'):
        args = args[1:]

        tag  = options.packages_tag

        def _out_pkg(prefix, bpkgs):
            bids = set()
            for bpkg in sorted(bpkgs):
                if hasattr(bpkg, 'signed'):
                    if bpkg.signed:
                        continue
                print(bpkg)
        bpkgs = koji_tag2pkgs(kapi, tag)
        bpkgs = koji_pkgs2archsigs(kapi, bpkgs)
        _out_pkg("Tag:", spkg.match_pkgs(args, bpkgs))
        sys.exit(0)
    elif args[0] in ('list-packages', 'list-pkgs', 'ls-pkgs'):
        args = args[1:]

        tag  = options.packages_tag
        comp = options.packages_compose

        def _out_pkg(prefix, bpkgs):
            prefix = "%8s" % prefix
            for bpkg in sorted(bpkgs):
                suffix = ''
                if hasattr(bpkg, 'stream') and bpkg.stream:
                    suffix += '(stream)'
                if hasattr(bpkg, '_koji_build_id'):
                    suffix += '(bid:%d)' % bpkg._koji_build_id
                if hasattr(bpkg, 'signed'):
                    if bpkg.signed:
                        suffix += '(sig:%s)' % bpkg.signed
                    else:
                        suffix += '(unsigned)'
                if spkg._is_branch_el8(bpkg):
                    suffix += '(branch)'
                if spkg._is_module(bpkg):
                    suffix += '(module)'
                if spkg._is_rebuild(bpkg):
                    suffix += '(rebuild)'
                print(prefix, bpkg, suffix)
        bpkgs = koji_tag2pkgs(kapi, tag)
        bpkgs = koji_pkgs2archsigs(kapi, bpkgs)
        _out_pkg("Tag:", spkg.match_pkgs(args, bpkgs))
        if comp is not None:
            cpkgs = composed_url2pkgs(comp)
            _out_pkg("Compose:", spkg.match_pkgs(args, cpkgs))
    elif args[0] in ('summary-packages', 'summary-pkgs', 'sum-pkgs'):
        args = args[1:]

        tag  = options.packages_tag
        comp = options.packages_compose

        bpkgs = koji_tag2pkgs(kapi, tag)
        print("  Tagged packages:", len(bpkgs))
        if args:
            print("  Matched:", len(spkg.match_pkgs(args, bpkgs)))
        if comp is not None:
            cpkgs = composed_url2pkgs(comp)
            print("Composed packages:", len(cpkgs))
            if args:
                print("  Matched:", len(spkg.match_pkgs(args, cpkgs)))
    elif args[0] in ('check-nvr', 'check-nvra'):

        tag  = options.packages_tag
        comp = options.packages_compose

        if args[0] == 'check-nvra':
            bpkg = spkg.nvra2pkg(args[1])
        else:
            bpkg = spkg.nvr2pkg(args[1])
        print("Pkg:", bpkg)
        if ml_pkgdeny.nvr(bpkg.name, bpkg.version, bpkg.release):
            print("Denied!")

        def _out_pkg(prefix, pkg, bpkgs, signed=False):
            prefix = "%8s" % prefix
            tpkgs = []
            for bpkg in sorted(bpkgs):
                if bpkg.name != pkg.name:
                    continue
                tpkgs.append(bpkg)
            if signed:
                tpkgs = sorted(koji_pkgs2archsigs(kapi, tpkgs))
            for bpkg in tpkgs:
                suffix = ''
                if hasattr(bpkg, 'stream') and bpkg.stream:
                    suffix += '(stream)'
                if hasattr(bpkg, '_koji_build_id'):
                    suffix += '(bid:%d)' % bpkg._koji_build_id
                if hasattr(bpkg, 'signed'):
                    if bpkg.signed:
                        suffix += '(sig:%s)' % bpkg.signed
                    else:
                        suffix += '(unsigned)'
                if spkg._is_branch_el8(bpkg):
                    suffix += '(branch)'
                if spkg._is_module(bpkg):
                    suffix += '(module)'
                if spkg._is_rebuild(bpkg):
                    suffix += '(rebuild)'
                if ml_gitdeny.nvr(bpkg.name, bpkg.version, bpkg.release):
                    suffix += '(git deny)'
                if False: pass
                elif bpkg.verGT(pkg):
                    print(prefix, "Newer:", bpkg, suffix)
                elif bpkg.verEQ(pkg):
                    print(prefix, "   EQ:", bpkg, suffix)
                elif bpkg.verLT(pkg):
                    print(prefix, "Older:", bpkg, suffix)
                else:
                    print(prefix, "!!:", bpkg, suffix)
        bpkgs = koji_tag2pkgs(kapi, tag)
        bpkgs = koji_pkgs2archsigs(kapi, bpkgs)
        _out_pkg("Tag:", bpkg, bpkgs, signed=True)
        if comp is not None:
            cpkgs = composed_url2pkgs(comp)
            _out_pkg("Compose:", bpkg, cpkgs)

        tcoroot = tempfile.TemporaryDirectory(prefix="sync2build-chk-", dir="/tmp")
        corootdir = tcoroot.name + '/'
        codir = corootdir + bpkg.name
        tags = bpkg2git_tags(bpkg, codir)
        if os.path.exists(codir + '/README.debrand'): # Doesn't work
            print(" ** Debranding **")
        tpkgs = _tags2pkgs(tags)
        _out_pkg("GIT:", bpkg, tpkgs)
    elif args[0] in ('build-nvr', 'build-nvra'):
        if args[0] == 'build-nvra':
            pkg = spkg.nvra2pkg(args[1])
        else:
            pkg = spkg.nvr2pkg(args[1])
        print("Pkg:", pkg)
        if not check_denylist_builds([pkg]):
            print("Pkg in denylist:", pkg)
            sys.exit(1) # Allow force?
        tcoroot = tempfile.TemporaryDirectory(prefix="sync2build-chk-", dir="/tmp")
        corootdir = tcoroot.name + '/'
        codir = corootdir + pkg.name
        tags = bpkg2git_tags(pkg, codir)
        tpkgs = _tags2pkgs(tags)
        found = False
        for tpkg in sorted(tpkgs):
            if tpkg.name != pkg.name:
                continue
            suffix = ''
            if hasattr(tpkg, 'stream') and tpkg.stream:
                suffix = '(stream)'
            if tpkg.verGT(pkg):
                print("Newer version in GIT, building that!", pkg, tpkg, suffix)
                pkg = tpkg
                found = True # Allow building older packages??
            elif tpkg.verEQ(pkg):
                pkg = tpkg
                found = True
                print("Found version in GIT:", tpkg, suffix)
        if not found:
            print("Didn't find (so can't build):", tpkg, suffix)
        else:
            bts = build_packages(kapi, [pkg], options.packages_tag)
            bts, dbts = bpids_wait_packages(kapi, bts, options.wait)
            bpids_print(bts)
            for bt in dbts: # Keep everything around for logs
                bts.append(bt)
            bpids_save(bts)

        sys.exit(0)

    elif args[0] in ('build-n', 'build-name'):
        pkgid = kapi.getPackageID(args[1])
        pkgs = koji_pkgid2pkgs(kapi, pkgid)
        for pkg in sorted(pkgs):
            print(pkg, pkg._koji_task_state, pkg._koji_build_id)

        sys.exit(0)

    elif args[0] in ('bpids-list', 'bipds'):
        tids = bpids_load(kapi)
        bpids_print(tids)
        sys.exit(0)

    elif args[0] in ('bpids-wait',):
        bts = bpids_load(kapi)
        bts, dbts = bpids_wait_packages(kapi, bts, options.wait)
        bpids_print(bts)
        bpids_save(bts)
        sys.exit(0)

    elif args[0] in ('tag-hash', 'tag-rpms-hash', 'tag-srpms-hash'):
        tag  = options.packages_tag
        csum = args[1]
        if args[0] == 'tag-srpms-hash':
            data = 's' + koji_tag2srpms_checksum(kapi, tag, csum)
        else:
            data = koji_tag2checksum(kapi, tag, csum)
        print(data)
        sys.exit(0)

    elif args[0] in ('packages', 'pkgs'):
        if not options.download_only:
            print(" ** Warning: This will build pkgs/mods in koji.")

        tag  = options.packages_tag
        comp = options.packages_compose
        bts = sync_packages(tag, comp, kapi)
        bts, dbts = bpids_wait_packages(kapi, bts, options.wait)
        bpids_print(bts)
        for bt in dbts:
            if bt.state == 'CLOSED': # Drop successes
                continue
             # Keep failures around for a bit...
            if bt.since > conf_ttl_failed_builds:
                continue
            bts.append(bt)
        bpids_save(bts)

    if not sys.stdout.isatty():
        print(" -- Done --")

# Badly written but working python script
if __name__ == "__main__":
    main()
