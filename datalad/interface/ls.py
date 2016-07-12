# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil; coding: utf-8 -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Helper utility to list things.  ATM list content of S3 bucket
"""

__docformat__ = 'restructuredtext'

import sys
import time
from os.path import exists, lexists, join as opj, abspath, isabs
from os.path import curdir, isfile, islink, isdir, dirname, basename, split
from os import readlink, listdir, lstat, remove
from json import dump, dumps

from six.moves.urllib.request import urlopen, Request
from six.moves.urllib.error import HTTPError

from ..utils import auto_repr
from .base import Interface
from ..ui import ui
from ..utils import swallow_logs
from ..dochelpers import exc_str
from ..support.s3 import get_key_url
from ..support.param import Parameter
from ..support.constraints import EnsureStr, EnsureNone
from ..distribution.dataset import Dataset
from datalad.cmd import Runner

from logging import getLogger
lgr = getLogger('datalad.api.ls')


class Ls(Interface):
    """List meta-information associated with URLs (e.g. s3://) and dataset(s)

    Examples
    --------

      $ datalad ls s3://openfmri/tarballs/ds202  # to list S3 bucket
      $ datalad ls                               # to list current dataset
    """

    _params_ = dict(
        loc=Parameter(
            doc="URL or path to list, e.g. s3://...",
            metavar='PATH/URL',
            nargs="*",
            constraints=EnsureStr() | EnsureNone(),
        ),
        recursive=Parameter(
            args=("-r", "--recursive"),
            action="store_true",
            doc="recurse into subdirectories",
        ),
        fast=Parameter(
            args=("-F", "--fast"),
            action="store_true",
            doc="only perform fast operations.  Would be overridden by --all",
        ),
        all=Parameter(
            args=("-a", "--all"),
            action="store_true",
            doc="list all entries, not e.g. only latest entries in case of S3",
        ),
        config_file=Parameter(
            doc="""path to config file which could help the 'ls'.  E.g. for s3://
            URLs could be some ~/.s3cfg file which would provide credentials""",
            constraints=EnsureStr() | EnsureNone()
        ),
        list_content=Parameter(
            choices=(None, 'first10', 'md5', 'full'),
            doc="""list also the content or only first 10 bytes (first10), or md5
            checksum of an entry.  Might require expensive transfer and dump
            binary output to your screen.  Do not enable unless you know what you
            are after""",
            default=None
        ),
        json=Parameter(
            choices=('file', 'display', 'delete'),
            doc="""metadata json of dataset for creating web user interface.
            display: prints jsons to stdout or
            file: writes each subdir metadata to json file in subdir of dataset or
            delete: deletes all metadata json files in dataset""",
        ),
    )

    @staticmethod
    def __call__(loc, recursive=False, fast=False, all=False, config_file=None, list_content=False, json=None):
        if isinstance(loc, list) and not len(loc):
            # nothing given, CWD assumed -- just like regular ls
            loc = '.'

        kw = dict(fast=fast, recursive=recursive, all=all)
        if isinstance(loc, list):
            return [Ls.__call__(loc_, config_file=config_file, list_content=list_content, json=json, **kw)
                    for loc_ in loc]

        # TODO: do some clever handling of kwargs as to remember what were defaults
        # and what any particular implementation actually needs, and then issuing
        # warning if some custom value/option was specified which doesn't apply to the
        # given url

        if loc.startswith('s3://'):
            return _ls_s3(loc, config_file=config_file, list_content=list_content, **kw)
        elif lexists(loc):  # and lexists(opj(loc, '.git')):
            # TODO: use some helper like is_dataset_path ??
            return _ls_json(loc, json=json, **kw) if json else _ls_dataset(loc, **kw)
        else:
            #raise ValueError("ATM supporting only s3:// URLs and paths to local datasets")
            # TODO: unify all the output here -- _ls functions should just return something
            # to be displayed
            ui.message(
                "%s%s%s  %sunknown%s"
                % (LsFormatter.BLUE, loc, LsFormatter.RESET, LsFormatter.RED, LsFormatter.RESET))


#
# Dataset listing
#

from datalad.support.annexrepo import AnnexRepo
from datalad.support.annexrepo import GitRepo


@auto_repr
class RepoBasedModel(object):
    """A base class for models which have some .repo available"""

    __slots__ = ['_info', '_branch']

    def __init__(self):
        # lazy evaluation variables
        self._branch = None
        self._info = None

    @property
    def branch(self):
        if self._branch is None:
            try:
                self._branch = self.repo.get_active_branch()
            except:
                return None
        return self._branch

    @property
    def describe(self):
        try:
            with swallow_logs():
                describe, outerr = self.repo._git_custom_command([], ['git', 'describe', '--tags'])
            return describe.strip()
        except:
            return None


    @property
    def date(self):
        """Date of the last commit
        """
        try:
            commit = next(self.repo.get_branch_commits(self.branch))
        except:
            return None
        return commit.committed_date

    @property
    def clean(self):
        return not self.repo.dirty

    @property
    def info(self):
        if self._info is None and isinstance(self.repo, AnnexRepo):
            self._info = self.repo.repo_info()
        return self._info

    @property
    def annex_worktree_size(self):
        info = self.info
        return info['size of annexed files in working tree'] if info else None

    @property
    def annex_local_size(self):
        info = self.info
        return info['local annex size'] if info else None

    @property
    def type(self):
        return {False: 'git', True: 'annex'}[isinstance(self.repo, AnnexRepo)]


@auto_repr
class DsModel(RepoBasedModel):

    __slots__ = RepoBasedModel.__slots__ + ['ds', '_path']

    def __init__(self, ds):
        super(DsModel, self).__init__()
        self.ds = ds
        # TODO:  theoretically should not be overriden and generally just be present in the ds
        self._path = None  # can be overriden

    @property
    def path(self):
        return self.ds.path if self._path is None else self._path

    @path.setter
    def path(self, v):
        self._path = v

    @property
    def repo(self):
        return self.ds.repo

    @property
    def type(self):
        if not exists(self.path):
            return None
        return super(DsModel, self).type


@auto_repr
class FsModel(RepoBasedModel):

    __slots__ = ['_path', 'repo'] + RepoBasedModel.__slots__

    def __init__(self, path, repo=None):
        super(FsModel, self).__init__()
        self._path = path  # fs path to the node, can be overridden
        self.repo = repo  # parent repository associated with node
        # of value only if it was annex
        # self.dsmodel = DsModel(Dataset(path)) if self.type_ == 'annex' else None

    @property
    def path(self):
        return self._path

    @property
    def symlink(self):
        """if symlink returns path the symlink points to else returns None"""
        if islink(self._path):                    # if symlink
            target_path = readlink(self._path)    # find link target
            # convert to absolute path if not
            target_path = opj(dirname(self._path), target_path) if not isabs(target_path) else target_path
            return target_path if exists(target_path) else None
        return None

    @property
    def date(self):
        """Date of last modification"""
        if self.type_ is not ['git', 'annex']:
            return lstat(self._path).st_mtime
        else:
            super(self.__class__, self).date

    @property
    def size(self):
        """Size of the node computed based on its type"""
        type_ = self.type_

        if type_ == 'annex':
            if self.annex_local_size:
                return self.annex_local_size
            else:
                return Runner().run(['du', '-s', self._path])[0].split('\t')[0]
        elif type_ == 'git':
            return self.git_local_size
        elif type_ in ['file', 'link', 'link-broken']:
            size = ondisk_size = 0 \
                if type_ == 'broken-link' \
                else lstat(self.symlink or self._path).st_size
            if isinstance(self.repo, AnnexRepo) and self.repo.is_under_annex(self._path):
                size = self.repo.info(self._path, batch=True)['size']
            # TODO: all the ondisk_size handling to report both
            return size
        elif 'dir' == type_:
            return Runner().run(['du', '-s', self._path])[0].split('\t')[0]  # lstat(self._path).st_size
        else:
            raise RuntimeError("I must have not got here! my type is %s" % type_)

    @property
    def type_(self):
        """outputs the node type

        Types: link, link-broken, file, dir, annex-repo, git-repo"""
        if islink(self.path):
            return 'link' if self.symlink else 'link-broken'
        elif isfile(self.path):
            return 'file'
        elif exists(opj(self.path, ".git", "annex")):
            return 'annex'
        elif exists(opj(self.path, ".git")):
            return 'git'
        elif isdir(self.path):
            return 'dir'
        else:
            return None

    @property
    def git_local_size(self):
        """computes the disk space used by unpacked object files in the git repository"""
        try:
            describe, outerr = self.repo._git_custom_command([], ['git', 'count-objects', '-v'])[0].split('\n')
            size = [item for item in describe if 'size: ' in item][0].split(': ')
            return int(size[1])
        except:
            return lstat(self._path).st_size


import string
import humanize
from datalad.log import ColorFormatter
from datalad.utils import is_interactive

class LsFormatter(string.Formatter):
    # condition by interactive
    if is_interactive():
        BLUE = ColorFormatter.COLOR_SEQ % (ColorFormatter.BLUE + 30)
        RED = ColorFormatter.COLOR_SEQ % (ColorFormatter.RED + 30)
        GREEN = ColorFormatter.COLOR_SEQ % (ColorFormatter.GREEN + 30)
        RESET = ColorFormatter.RESET_SEQ
    else:
        BLUE = RED = GREEN = RESET = u""

    # http://stackoverflow.com/questions/9932406/unicodeencodeerror-only-when-running-as-a-cron-job
    # reveals that Python uses ascii encoding when stdout is a pipe, so we shouldn't force it to be
    # unicode then
    # TODO: we might want to just ignore and force utf8 while explicitly .encode()'ing output!
    if sys.getdefaultencoding() == 'ascii':
        OK = 'OK'   # u"✓"
        NOK = 'X'  # u"✗"
        NONE = '-'  # u"✗"
    else:
        # unicode versions which look better but which blow during tests etc
        OK = u"✓"
        NOK = u"✗"
        NONE = u"✗"

    def convert_field(self, value, conversion):
        #print("%r->%r" % (value, conversion))
        if conversion == 'D':  # Date
            if value is not None:
                return time.strftime(u"%Y-%m-%d/%H:%M:%S", time.localtime(value))
            else:
                return u'-'
        elif conversion == 'S':  # Human size
            #return value
            if value is not None:
                return humanize.naturalsize(value)
            else:
                return u'-'
        elif conversion == 'X':  # colored bool
            chr, col = (self.OK, self.GREEN) if value else (self.NOK, self.RED)
            return u"%s%s%s" % (col, chr, self.RESET)
        elif conversion == 'N':  # colored Red - if None
            if value is None:
                # return "%s✖%s" % (self.RED, self.RESET)
                return u"%s%s%s" % (self.RED, self.NONE, self.RESET)
            return value
        elif conversion in {'B', 'R'}:
            return u"%s%s%s" % ({'B': self.BLUE, 'R': self.RED}[conversion], value, self.RESET)

        return super(LsFormatter, self).convert_field(value, conversion)


def format_ds_model(formatter, ds_model, format_str, format_exc):
    try:
        #print("WORKING ON %s" % ds_model.path)
        if not exists(ds_model.ds.path) or not ds_model.ds.repo:
            return formatter.format(format_exc, ds=ds_model, msg=u"not installed")
        ds_formatted = formatter.format(format_str, ds=ds_model)
        #print("FINISHED ON %s" % ds_model.path)
        return ds_formatted
    except Exception as exc:
        return formatter.format(format_exc, ds=ds_model, msg=exc_str(exc))

# from joblib import Parallel, delayed

def _ls_dataset(loc, fast=False, recursive=False, all=False):
    isabs_loc = isabs(loc)
    topdir = '' if isabs_loc else abspath(curdir)

    topds = Dataset(loc)
    dss = [topds] + (
        [Dataset(opj(loc, sm))
         for sm in topds.get_subdatasets(recursive=recursive)]
        if recursive else [])
    dsms = list(map(DsModel, dss))

    # adjust path strings
    for ds_model in dsms:
        path = ds_model.path[len(topdir) + 1 if topdir else 0:]
        if not path:
            path = '.'
        ds_model.path = path
    dsms = sorted(dsms, key=lambda m: m.path)

    maxpath = max(len(ds_model.path) for ds_model in dsms)
    path_fmt = u"{ds.path!B:<%d}" % (maxpath + (11 if is_interactive() else 0))  # + to accommodate ansi codes
    pathtype_fmt = path_fmt + u"  [{ds.type}]"
    full_fmt = pathtype_fmt + u"  {ds.branch!N}  {ds.describe!N} {ds.date!D}"
    if (not fast) or all:
        full_fmt += u"  {ds.clean!X}"
    if all:
        full_fmt += u"  {ds.annex_local_size!S}/{ds.annex_worktree_size!S}"

    formatter = LsFormatter()
    # weird problems happen in the parallel run -- TODO - figure it out
    # for out in Parallel(n_jobs=1)(
    #         delayed(format_ds_model)(formatter, dsm, full_fmt, format_exc=path_fmt + "  {msg!R}")
    #         for dsm in dss):
    #     print(out)
    for dsm in dsms:
        ds_str = format_ds_model(formatter, dsm, full_fmt, format_exc=path_fmt + u"  {msg!R}")
        print(ds_str)


def fs_extract(nodepath, repo):
    """extract required info of nodepath with its associated parent repository and returns it as a dictionary"""

    # Create FsModel from filesystem nodepath and its associated parent repository
    node = FsModel(nodepath, repo)
    pretty_size = humanize.naturalsize(node.size) if node.size else -1
    pretty_date = time.strftime(u"%Y-%m-%d %H:%M:%S", time.localtime(node.date))
    name = leaf_name(node._path) if leaf_name(node._path) != "" else leaf_name(node.repo.path)
    return {"name": name, "path": node._path, "repo": node.repo.path, "type": node.type_, "size": pretty_size, "date": pretty_date}


def leaf_name(path):
    """takes a relative or absolute path and returns name of node at that location"""
    head, tail = split(abspath(path))
    return tail or basename(head)


def ignored(path, only_hidden=False):
    """if path is in the ignorelist return True

    ignore list includes hidden files and git or annex maintained folders
    when only_hidden set, only ignores hidden files and folders not git or annex maintained folders
    """
    if (isdir(opj(path, ".git")) or isdir(opj(path, ".git", "annex"))) and not only_hidden:
        return True
    if '.' == leaf_name(path)[0] or leaf_name(path) == 'index.html':
        return True
    return False


def fs_render(root, subdir, json=None):
    """takes root, subdir to render and based on json option passed renders to file, stdout or deletes json at root"""
    # store directory info of the submodule level in fs hierarchy as json
    if json == 'file':
        with open(opj(root, '.dir.json'), 'w') as f:
            dump(subdir, f)
    # else if json flag set to delete, remove .dir.json of current directory
    elif json == 'delete':
        if exists(opj(root, '.dir.json')):
            remove(opj(root, '.dir.json'))
    # else dump json to stdout
    elif json == 'display':
        print dumps(subdir) + '\n'


def fs_traverse(path, repo, recursive=False, json=None):
    """Traverse path through its nodes and returns a dictionary of relevant attributes attached to each node

    Parameters
    ----------
    path
    repo
    recursive: bool
      Recurse into subdirectories (note that submodules are not traversed)
    json

    Returns
    -------
    list of dict
      extracts and returns a (recursive) list of directory info at path
      does not traverse into annex, git or hidden directories
    """
    fs = fs_extract(path, repo)

    if isdir(path):                                # if node is a directory
        fs["nodes"] = [fs_extract(path, repo)]     # store its info in dict
        fs["nodes"][0]["name"] = ".."             # and replace its name with ".." to emulate unix syntax

        for node in listdir(path):
            nodepath = opj(path, node)
            if not ignored(nodepath, only_hidden=True):
                # append info on nodes children to its dictionary
                fs["nodes"].extend([fs_extract(nodepath, repo)])

            if recursive and isdir(nodepath) and not ignored(nodepath):
                # if recursive, create info dictionary of each child directory
                subdir = fs_traverse(nodepath, repo, recursive=recursive, json=json)
                # run renderer on subdirectory(subdir) at location(path) with json option set by user
                lgr.info('Subdir: ' + opj(path, node))
                fs_render(nodepath, subdir, json=json)

        # update current node size by summing sizes of all its 1st level children
        total_size = reduce(lambda size, node: size + int(FsModel(node['path'], repo).size),
                            fs['nodes'][1:],
                            0)
        fs["size"], fs["nodes"][0]["size"] = [humanize.naturalsize(total_size)] * 2

    return fs


def _ls_json(loc, json=None, fast=False, recursive=False, all=False):
    # find all sub-datasets under path passed and attach Dataset class to each
    topds = Dataset(loc)
    dss = [topds] + (
        [Dataset(opj(loc, sm))
         for sm in topds.get_subdatasets(recursive=recursive)]
        if recursive else [])
    dsms = list(map(DsModel, dss))

    # for each submodule at loc passed by user
    for ds in dsms:
        # (recursively) traverse each submodule
        fs = fs_traverse(opj(ds.path, ""), ds, recursive=recursive, json=json)
        # run renderer on submodule(fs) at ds.path with json option set by user
        lgr.info('Submodule: ' + opj(ds.path))
        fs_render(ds.path, fs, json=json)


#
# S3 listing
#
def _ls_s3(loc, fast=False, recursive=False, all=False, config_file=None, list_content=False):
    """List S3 bucket content"""
    if loc.startswith('s3://'):
        bucket_prefix = loc[5:]
    else:
        raise ValueError("passed location should be an s3:// url")

    import boto
    from hashlib import md5
    from boto.s3.key import Key
    from boto.s3.prefix import Prefix
    from boto.exception import S3ResponseError
    from ..support.configparserinc import SafeConfigParser  # provides PY2,3 imports

    bucket_name, prefix = bucket_prefix.split('/', 1)

    if '?' in prefix:
        ui.message("We do not care about URL options ATM, they get stripped")
        prefix = prefix[:prefix.index('?')]

    ui.message("Connecting to bucket: %s" % bucket_name)
    if config_file:
        config = SafeConfigParser(); config.read(config_file)
        access_key = config.get('default', 'access_key')
        secret_key = config.get('default', 'secret_key')

        # TODO: remove duplication -- reuse logic within downloaders/s3.py to get connected
        conn = boto.connect_s3(access_key, secret_key)
        try:
            bucket = conn.get_bucket(bucket_name)
        except S3ResponseError as e:
            ui.message("E: Cannot access bucket %s by name" % bucket_name)
            all_buckets = conn.get_all_buckets()
            all_bucket_names = [b.name for b in all_buckets]
            ui.message("I: Found following buckets %s" % ', '.join(all_bucket_names))
            if bucket_name in all_bucket_names:
                bucket = all_buckets[all_bucket_names.index(bucket_name)]
            else:
                raise RuntimeError("E: no bucket named %s thus exiting" % bucket_name)
    else:
        # TODO: expose credentials
        # We don't need any provider here really but only credentials
        from datalad.downloaders.providers import Providers
        providers = Providers.from_config_files()
        provider = providers.get_provider(loc)
        if not provider:
            raise ValueError("don't know how to deal with this url %s -- no downloader defined.  "
                             "Specify just s3cmd config file instead")
        bucket = provider.authenticator.authenticate(bucket_name, provider.credential)

    info = []
    for iname, imeth in [
        ("Versioning", bucket.get_versioning_status),
        ("   Website", bucket.get_website_endpoint),
        ("       ACL", bucket.get_acl),
    ]:
        try:
            ival = imeth()
        except Exception as e:
            ival = str(e).split('\n')[0]
        info.append(" {iname}: {ival}".format(**locals()))
    ui.message("Bucket info:\n %s" % '\n '.join(info))

    kwargs = {} if recursive else {'delimiter': '/'}

    ACCESS_METHODS = [
        bucket.list_versions,
        bucket.list
    ]

    prefix_all_versions = None
    for acc in ACCESS_METHODS:
        try:
            prefix_all_versions = list(acc(prefix, **kwargs))
            break
        except Exception as exc:
            lgr.debug("Failed to access via %s: %s", acc, exc_str(exc))

    if not prefix_all_versions:
        ui.error("No output was provided for prefix %r" % prefix)
    else:
        max_length = max((len(e.name) for e in prefix_all_versions))
        max_size_length = max((len(str(getattr(e, 'size', 0))) for e in prefix_all_versions))

    for e in prefix_all_versions:
        if isinstance(e, Prefix):
            ui.message("%s" % (e.name, ),)
            continue
        ui.message(("%%-%ds %%s" % max_length) % (e.name, e.last_modified), cr=' ')
        if isinstance(e, Key):
            ui.message(" %%%dd" % max_size_length % e.size, cr=' ')
            if not (e.is_latest or all):
                # Skip this one
                ui.message("")
                continue
            url = get_key_url(e, schema='http')
            try:
                _ = urlopen(Request(url))
                urlok = "OK"
            except HTTPError as err:
                urlok = "E: %s" % err.code

            try:
                acl = e.get_acl()
            except S3ResponseError as err:
                acl = err.message

            content = ""
            if list_content:
                # IO intensive, make an option finally!
                try:
                    # _ = e.next()[:5]  if we are able to fetch the content
                    kwargs = dict(version_id=e.version_id)
                    if list_content in {'full', 'first10'}:
                        if list_content in 'first10':
                            kwargs['headers'] = {'Range': 'bytes=0-9'}
                        content = repr(e.get_contents_as_string(**kwargs))
                    elif list_content == 'md5':
                        digest = md5()
                        digest.update(e.get_contents_as_string(**kwargs))
                        content = digest.hexdigest()
                    else:
                        raise ValueError(list_content)
                    # content = "[S3: OK]"
                except S3ResponseError as err:
                    content = err.message
                finally:
                    content = " " + content

            ui.message("ver:%-32s  acl:%s  %s [%s]%s" % (e.version_id, acl, url, urlok, content))
        else:
            ui.message(str(type(e)).split('.')[-1].rstrip("\"'>"))
