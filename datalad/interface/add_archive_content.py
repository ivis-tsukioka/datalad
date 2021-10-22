# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""High-level interface for adding content of an archive under annex control

"""
from __future__ import print_function

__docformat__ = 'restructuredtext'


import os
import re
import tempfile
import warnings
from os.path import join as opj
from os.path import (
    basename,
    exists,
    lexists,
    curdir
)
from os.path import sep as opsep

from datalad.consts import ARCHIVES_SPECIAL_REMOTE
from datalad.customremotes.base import init_datalad_remote
from datalad.distribution.dataset import (
    EnsureDataset,
    datasetmethod,
    require_dataset,
    resolve_path,
)
from datalad.interface.base import build_doc
from datalad.interface.utils import eval_results
from datalad.interface.results import get_status_dict
from datalad.log import logging
from datalad.support.annexrepo import AnnexRepo
from datalad.support.constraints import (
    EnsureNone,
    EnsureStr,
)
from datalad.support.param import Parameter
from datalad.support.stats import ActivityStats
from datalad.support.strings import apply_replacement_rules
from datalad.utils import (
    Path,
    ensure_tuple_or_list,
    file_basename,
    getpwd,
    md5sum,
    rmtree,
    split_cmdline,
)

from .base import Interface
from .common_opts import allow_dirty

lgr = logging.getLogger('datalad.interfaces.add_archive_content')


# Shortcut note
_KEY_OPT = "[PY: `key=True` PY][CMD: --key CMD]"
_KEY_OPT_NOTE = "Note that it will be of no effect if %s is given" % _KEY_OPT

# TODO: may be we could enable separate logging or add a flag to enable
# all but by default to print only the one associated with this given action


@build_doc
class AddArchiveContent(Interface):
    """Add content of an archive under git annex control.

    Given an archive under annex control, extract and add its files to the
    dataset, and reference the original archive as a custom special remote.

    """
    _examples_ = [
        dict(text="""Add files from the archive 'big_tarball.tar.gz', but
                     keep big_tarball.tar.gz in the index""",
             code_py="add_archive_content(path='big_tarball.tar.gz')",
             code_cmd="datalad add-archive-content big_tarball.tar.gz"),
        dict(text="""Add files from the archive 'tarball.tar.gz', and
                     remove big_tarball.tar.gz from the index""",
             code_py="add_archive_content(path='big_tarball.tar.gz', delete=True)",
             code_cmd="datalad add-archive-content big_tarball.tar.gz --delete"),
        dict(text="""Add files from the archive 's3.zip' but remove the leading
                     directory""",
             code_py="add_archive_content(path='s3.zip', strip_leading_dirs=True)",
             code_cmd="datalad add-archive-content s3.zip --strip-leading-dirs"),
        ]

    # XXX prevent common args from being added to the docstring
    _no_eval_results = True
    _params_ = dict(
        dataset=Parameter(
            args=("-d", "--dataset"),
            doc=""""specify the dataset to save""",
            constraints=EnsureDataset() | EnsureNone()),
        delete=Parameter(
            args=("-D", "--delete"),
            action="store_true",
            doc="""delete original archive from the filesystem/Git in current
            tree. %s""" % _KEY_OPT_NOTE),
        add_archive_leading_dir=Parameter(
            args=("--add-archive-leading-dir",),
            action="store_true",
            doc="""place extracted content under a directory which would
            correspond to the archive name with suffix stripped. E.g. the
            content of `example.zip` will be extracted under `example/`"""),
        strip_leading_dirs=Parameter(
            args=("--strip-leading-dirs",),
            action="store_true",
            doc="""move all archive contents up from how they are stored in the
             archive, if the archive contains a number (possibly more than 1
             down) single leading directories"""),
        leading_dirs_depth=Parameter(
            args=("--leading-dirs-depth",),
            action="store",
            type=int,
            doc="""maximal depth to strip leading directories to.
            If not specified (None), no limit"""),
        leading_dirs_consider=Parameter(
            args=("--leading-dirs-consider",),
            action="append",
            doc="""regular expression(s) for directories to consider to strip
            away""",
            constraints=EnsureStr() | EnsureNone(),
        ),
        use_current_dir=Parameter(
            args=("--use-current-dir",),
            action="store_true",
            doc="""extract the archive under the current directory, not the
             directory where archive is located. %s""" % _KEY_OPT_NOTE),
        # TODO: add option to extract under archive's original directory. Currently would extract in curdir
        existing=Parameter(
            args=("--existing",),
            choices=('fail', 'overwrite', 'archive-suffix', 'numeric-suffix'),
            default="fail",
            doc="""what operation to perform if a file from an archive tries to
            overwrite an existing file with the same name.  'fail' (default)
            leads to RuntimeError exception, 'overwrite' silently replaces
            existing file, 'archive-suffix' instructs to add a suffix (prefixed
            with a '-') matching archive name from which file gets extracted,
            and if that one is present as well, 'numeric-suffix' is in effect in
            addition, when incremental numeric suffix (prefixed with a '.') is
            added until no name collision is longer detected"""
        ),
        exclude=Parameter(
            args=("-e", "--exclude"),
            action='append',
            doc="""regular expressions for filenames which to exclude from being
            added to annex. Applied after --rename if that one is specified.
            For exact matching, use anchoring""",
            constraints=EnsureStr() | EnsureNone()
        ),
        rename=Parameter(
            args=("-r", "--rename"),
            action='append',
            doc="""regular expressions to rename files before added them under
            to Git. The first defines how to split provided string into
            two parts: Python regular expression (with groups), and replacement
            string""",
            constraints=EnsureStr(min_len=2) | EnsureNone()
        ),
        annex_options=Parameter(
            args=("-o", "--annex-options"),
            doc="""additional options to pass to git-annex. """,
            constraints=EnsureStr() | EnsureNone()
        ),
        annex=Parameter(
            doc="""annex instance to use. This parameter will
            be deprecated. Use the 'dataset' parameter instead."""
        ),
        # TODO: Python only!
        stats=Parameter(
            doc="""ActivityStats instance for global tracking""",
        ),
        key=Parameter(
            args=("--key",),
            action="store_true",
            doc="""signal if provided archive is not actually a filename on its
            own but an annex key"""),
        copy=Parameter(
            args=("--copy",),
            action="store_true",
            doc="""copy the content of the archive instead of moving"""),
        allow_dirty=allow_dirty,
        commit=Parameter(
            args=("--no-commit",),
            action="store_false",
            dest="commit",
            doc="""don't commit upon completion"""),
        drop_after=Parameter(
            args=("--drop-after",),
            action="store_true",
            doc="""drop extracted files after adding to annex""",
        ),
        delete_after=Parameter(
            args=("--delete-after",),
            action="store_true",
            doc="""extract under a temporary directory, git-annex add, and
            delete afterwards. To be used to "index" files within annex without
            actually creating corresponding files under git. Note that
            `annex dropunused` would later remove that load"""),

        # TODO: interaction with archives cache whenever we make it persistent across runs
        archive=Parameter(
            doc="archive file or a key (if %s specified)" % _KEY_OPT,
            constraints=EnsureStr()),
    )

        # use-case from openfmri pipeline
        #     ExtractArchives(
        #         # will do the merge of 'replace' strategy
        #         source_branch="incoming",
        #         regex="\.(tgz|tar\..*)$",
        #         renames=[
        #             ("^[^/]*/(.*)", "\1") # e.g. to strip leading dir, or could prepend etc
        #         ],
        #         #exclude="license.*",  # regexp
        #     ),

    @staticmethod
    @datasetmethod(name='add_archive_content')
    @eval_results
    def __call__(
            archive,
            dataset=None,
            annex=None,
            add_archive_leading_dir=False,
            strip_leading_dirs=False,
            leading_dirs_depth=None,
            leading_dirs_consider=None,
            use_current_dir=False,
            delete=False,
            key=False,
            exclude=None,
            rename=None,
            existing='fail',
            annex_options=None,
            copy=False,
            commit=True,
            allow_dirty=False,
            stats=None,
            drop_after=False,
            delete_after=False):

        if exclude:
            exclude = ensure_tuple_or_list(exclude)
        if rename:
            rename = ensure_tuple_or_list(rename)
        ds = require_dataset(dataset,
                             check_installed=True,
                             purpose='add-archive-content')

        # set up common params for result records
        res_kwargs = {
            'action': 'add-archive-content',
            'logger': lgr,
        }

        if not isinstance(ds.repo, AnnexRepo):
            yield get_status_dict(
                ds=ds,
                status='impossible',
                message="Can't operate in a pure Git repository",
                **res_kwargs
            )
            return
        if annex:
            warnings.warn(
                "datalad add_archive_content's `annex` parameter is "
                "deprecated and will be removed in a future release. "
                "Use the 'dataset' parameter instead.",
                DeprecationWarning)
        annex = ds.repo
        # get the archive path relative from the ds root
        archive_path = resolve_path(archive, ds=dataset)
        # let Status decide whether we can act on the given file
        for s in ds.status(
                path=archive_path,
                report_filetype=False,
                on_failure='ignore',
                result_renderer='disabled'):
            if s['status'] == 'error':
                if s['message'] == 'path not underneath this dataset':
                    yield get_status_dict(
                        ds=ds,
                        status='impossible',
                        message='Can not add archive outside of the dataset',
                        **res_kwargs)
                    return
                # status errored & we haven't anticipated the cause. Bubble up
                yield s
                return
            elif s['state'] == 'untracked':
                # we can't act on an untracked file
                message = (
                    "Can not add an untracked archive. "
                    "Run 'datalad save {}'".format(archive)
                )
                yield get_status_dict(
                           ds=ds,
                           status='impossible',
                           message=message,
                           **res_kwargs)
                return

        if not allow_dirty and annex.dirty:
            # error out here if the dataset contains untracked changes
            yield get_status_dict(
                ds=ds,
                status='impossible',
                message=(
                    'clean dataset required. '
                    'Use `datalad status` to inspect unsaved changes'),
                **res_kwargs
            )
            return

        # ensure the archive exists, status doesn't error on a non-existing file
        if not key and not lexists(archive_path):
            yield get_status_dict(
                ds=ds,
                status='impossible',
                message=(
                    'No such file: {}'.format(archive_path),
                ),
                **res_kwargs
            )
            return

        if str(archive_path.relative_to(annex.path)) not in \
                annex.get_annexed_files():
            raise RuntimeError(
                "Can not add an archive that is not under annex control.")

        if not key:
            # we were given a file which must exist
            # TODO: support adding archives content from outside the annex/repo
            origin = 'archive'
            files = annex.get_content_annexinfo([archive_path], init=None)
            key = files[archive_path]['key']
            archive_dir = Path(archive_path).parent
        else:
            origin = 'key'
            key = archive
            # We must not have anything to do with the location under .git/annex
            archive_dir = None

        archive_basename = file_basename(archive)

        if not key:
            # if we didn't manage to get a key, the file must be in Git
            raise NotImplementedError(
                "Provided file %s does not seem to be under annex control. "
                "We don't support adding everything straight to Git" % archive
            )

        # figure out our location
        pwd = getpwd()
        # are we in a subdirectory of the repository?
        pwd_in_root = annex.path == archive_dir

        #  then we should add content under that
        # subdirectory,
        # get the path relative to the repo top
        if use_current_dir:
            # if outside -- extract to the top of repo
            extract_rpath = Path(pwd).relative_to(annex.path) \
                if not pwd_in_root \
                else None
        else:
            extract_rpath = archive_dir.relative_to(annex.path)

        # TODO
        # relpath might return '.' as the relative path to curdir, which then normalize_paths
        # would take as instructions to really go from cwd, so we need to sanitize
        if extract_rpath == curdir:
            extract_rpath = None

        try:
            key_rpath = annex.get_contentlocation(key)
        except:
            # the only probable reason for this to fail is that there is no
            # content present
            raise RuntimeError(
                "Content of %s seems to be N/A.  Fetch it first" % key
            )

        # now we simply need to go through every file in that archive and
        lgr.info(
            "Adding content of the archive %s into annex %s", archive, annex
        )

        from datalad.customremotes.archives import ArchiveAnnexCustomRemote
        # TODO: shouldn't we be able just to pass existing AnnexRepo instance?
        # TODO: we will use persistent cache so we could just (ab)use possibly extracted archive
        annexarchive = ArchiveAnnexCustomRemote(path=annex.path,
                                                persistent_cache=True)
        # We will move extracted content so it must not exist prior running
        annexarchive.cache.allow_existing = True
        earchive = annexarchive.cache[key_rpath]

        # TODO: check if may be it was already added
        if ARCHIVES_SPECIAL_REMOTE not in annex.get_remotes():
            init_datalad_remote(annex, ARCHIVES_SPECIAL_REMOTE, autoenable=True)
        else:
            # no need to init the special remote, it already exists
            lgr.debug("Special remote {} already exists".format(ARCHIVES_SPECIAL_REMOTE))
        precommitted = False
        delete_after_rpath = None
        try:
            old_always_commit = annex.always_commit
            # When faking dates, batch mode is disabled, so we want to always
            # commit.
            annex.always_commit = annex.fake_dates_enabled

            if annex_options:
                if isinstance(annex_options, str):
                    annex_options = split_cmdline(annex_options)

            # move archive contents up if the archive contains single leading
            # directories and strip_leading_dirs is set
            leading_dir = earchive.get_leading_directory(
                depth=leading_dirs_depth, exclude=exclude,
                consider=leading_dirs_consider) \
                if strip_leading_dirs else None
            # This seems to count the number of characters in the leading dir
            leading_dir_len = \
                len(leading_dir) + len(opsep) if leading_dir else 0


            # we need to create a temporary directory at the top level which
            # would later be removed
            prefix_dir = basename(tempfile.mktemp(prefix=".datalad",
                                                  dir=annex.path)) \
                if delete_after \
                else None
            archive_rpath = archive_path.relative_to(ds.path)
            # dedicated stats which would be added to passed in (if any)
            outside_stats = stats
            stats = ActivityStats()
            extracted_files = list(earchive.get_extracted_files())
            for extracted_file in extracted_files:
                stats.files += 1
                extracted_path = Path(earchive.path) / Path(extracted_file)

                if extracted_path.is_symlink():
                    link_path = str(extracted_path.resolve())
                    if not exists(link_path):
                        # TODO: config  addarchive.symlink-broken='skip'
                        lgr.warning(
                            "Path %s points to non-existing file %s" %
                            (extracted_path, link_path)
                        )
                        stats.skipped += 1
                        continue
                        # TODO: check if points outside of archive - warn & skip

                # preliminary target name which might get modified by renames
                target_file_orig = target_file = Path(extracted_file)

                # stream archives would not have had the original filename
                # information in them, so would be extracted under a name
                # derived from their annex key.
                # Provide ad-hoc handling for such cases
                if (len(extracted_files) == 1 and
                    Path(archive).suffix in ('.xz', '.gz', '.lzma') and
                        Path(key_rpath).name.startswith(Path(
                            extracted_file).name)):
                    # take archive's name without extension for filename & place
                    # where it was originally extracted
                    target_file = \
                        Path(extracted_file).parent / Path(archive).stem
                # strip leading dirs
                target_file = str(target_file)[leading_dir_len:]

                if add_archive_leading_dir:
                    # place extracted content under a directory corresponding to
                    # the archive name with suffix stripped.
                    target_file = Path(archive_basename) / target_file

                if rename:
                    target_file = apply_replacement_rules(rename,
                                                          str(target_file))

                # continue to next iteration if extracted_file in excluded
                if exclude:
                    try:  # since we need to skip outside loop from inside loop
                        for regexp in exclude:
                            if re.search(regexp, extracted_file):
                                lgr.debug(
                                    "Skipping {extracted_file} since contains "
                                    "{regexp} pattern".format(**locals()))
                                stats.skipped += 1
                                raise StopIteration
                    except StopIteration:
                        continue

                if prefix_dir:
                    # place target file in a temporary directory
                    target_file = Path(prefix_dir) / Path(target_file)
                    # but also allow for it in the orig
                    target_file_orig = Path(prefix_dir) / Path(target_file_orig)

                target_file_path_orig = annex.pathobj / target_file_orig

                url = annexarchive.get_file_url(
                    archive_key=key,
                    file=extracted_file,
                    size=os.stat(extracted_path).st_size)

                # lgr.debug("mv {extracted_path} {target_file}. URL: {url}".format(**locals()))

                target_file_path = extract_rpath / target_file \
                    if extract_rpath else target_file

                target_file_path = annex.pathobj / target_file_path

                if lexists(target_file_path):
                    handle_existing = True
                    if md5sum(str(target_file_path)) == \
                            md5sum(str(extracted_path)):
                        if not annex.is_under_annex(str(extracted_path)):
                            # if under annex -- must be having the same content,
                            # we should just add possibly a new extra URL
                            # but if under git -- we cannot/should not do
                            # anything about it ATM
                            if existing != 'overwrite':
                                continue
                        else:
                            handle_existing = False
                    if not handle_existing:
                        pass  # nothing... just to avoid additional indentation
                    elif existing == 'fail':
                        raise RuntimeError(
                            "{} already exists, but new file {} was instructed "
                            "to be placed there while overwrite=False".format
                            (target_file_path, extracted_file)
                        )
                    elif existing == 'overwrite':
                        stats.overwritten += 1
                        # to make sure it doesn't conflict -- might have been a
                        # tree
                        rmtree(target_file_path)
                    else:
                        target_file_path_orig_ = target_file_path

                        # To keep extension intact -- operate on the base of the
                        # filename
                        p, fn = os.path.split(target_file_path)
                        ends_with_dot = fn.endswith('.')
                        fn_base, fn_ext = file_basename(fn, return_ext=True)

                        if existing == 'archive-suffix':
                            fn_base += '-%s' % archive_basename
                        elif existing == 'numeric-suffix':
                            pass  # archive-suffix will have the same logic
                        else:
                            raise ValueError(existing)
                        # keep incrementing index in the suffix until file
                        # doesn't collide
                        suf, i = '', 0
                        while True:
                            connector = \
                                ('.' if (fn_ext or ends_with_dot) else '')
                            file = fn_base + suf + connector + fn_ext
                            target_file_path_new =  \
                                Path(p) / Path(file)
                            if not lexists(target_file_path_new):
                                break
                            lgr.debug("File %s already exists",
                                      target_file_path_new)
                            i += 1
                            suf = '.%d' % i
                        target_file_path = target_file_path_new
                        lgr.debug("Original file %s will be saved into %s"
                                  % (target_file_path_orig_, target_file_path))
                        # TODO: should we reserve smth like
                        # stats.clobbed += 1

                if target_file_path != target_file_path_orig:
                    stats.renamed += 1

                #target_path = opj(getpwd(), target_file)
                if copy:
                    raise NotImplementedError(
                        "Not yet copying from 'persistent' cache"
                    )
                else:
                    # os.renames(extracted_path, target_path)
                    # addurl implementation relying on annex'es addurl below
                    # would actually copy
                    pass

                lgr.debug("Adding %s to annex pointing to %s and with options "
                          "%r", target_file_path, url, annex_options)

                out_json = annex.add_url_to_file(
                    target_file_path,
                    url, options=annex_options,
                    batch=True)

                if 'key' in out_json and out_json['key'] is not None:
                    # annex.is_under_annex(target_file, batch=True):
                    # due to http://git-annex.branchable.com/bugs/annex_drop_is_not___34__in_effect__34___for_load_which_was___34__addurl_--batch__34__ed_but_not_yet_committed/?updated
                    # we need to maintain a list of those to be dropped files
                    if drop_after:
                        # drop extracted files after adding to annex
                        annex.drop_key(out_json['key'], batch=True)
                        stats.dropped += 1
                    stats.add_annex += 1
                else:
                    lgr.debug("File {} was added to git, not adding url".format(
                        target_file_path))
                    stats.add_git += 1

                if delete_after:
                    # delayed removal so it doesn't interfer with batched
                    # processes since any pure Git action invokes precommit
                    # which closes batched processes. But we like to count
                    stats.removed += 1

                # # chaining 3 annex commands, 2 of which not batched -- less
                # efficient but more bullet proof etc
                # annex.add(target_path, options=annex_options)
                # # above action might add to git or to annex
                # if annex.file_has_content(target_path):
                #     # if not --  it was added to git, if in annex, it is
                #     # present and output is True
                #     annex.add_url_to_file(target_file, url,
                #                           options=['--relaxed'], batch=True)
                #     stats.add_annex += 1
                # else:
                #     lgr.debug("File {} was added to git, not adding
                #                url".format(target_file))
                #     stats.add_git += 1
                # # TODO: actually check if it is anyhow different from a
                # # previous version. If not then it wasn't really added

                # Done with target_file -- just to have clear end of the loop
                del target_file

            if delete and archive and origin != 'key':
                lgr.debug("Removing the original archive {}".format(archive))
                # force=True since some times might still be staged and fail
                annex.remove(str(archive_path), force=True)

            lgr.info("Finished adding %s: %s", archive, stats.as_str(mode='line'))

            if outside_stats:
                outside_stats += stats
            if delete_after:
                # force since not committed. r=True for -r (passed into git call
                # to recurse)
                delete_after_rpath = opj(extract_rpath, prefix_dir) \
                    if extract_rpath else prefix_dir
                delete_after_rpath = resolve_path(delete_after_rpath,
                                                  ds=dataset)
                lgr.debug(
                    "Removing extracted and annexed files under %s",
                    delete_after_rpath
                )
                annex.remove(str(delete_after_rpath), r=True, force=True)
            if commit:
                commit_stats = outside_stats if outside_stats else stats
                # so batched ones close and files become annex symlinks etc
                annex.precommit()
                precommitted = True
                if any(r.get('state', None) != 'clean'
                       for p, r in annex.status(untracked='no').items()):
                    annex.commit(
                        "Added content extracted from %s %s\n\n%s" %
                        (origin, archive_rpath,
                         commit_stats.as_str(mode='full')),
                        _datalad_msg=True
                    )
                    commit_stats.reset()
        finally:
            # since we batched addurl, we should close those batched processes
            # if haven't done yet.  explicitly checked to avoid any possible
            # "double-action"
            if not precommitted:
                annex.precommit()

            if delete_after_rpath:
                delete_after_path = opj(annex.path, delete_after_rpath)
                delete_after_rpath = resolve_path(delete_after_rpath,
                                                  ds=dataset)
                if exists(delete_after_path):  # should not be there
                    # but for paranoid yoh
                    lgr.warning(
                        "Removing temporary directory under which extracted "
                        "files were annexed and should have been removed: %s",
                        delete_after_path)
                    rmtree(delete_after_path)

            annex.always_commit = old_always_commit
            # remove what is left and/or everything upon failure
            earchive.clean(force=True)

        yield get_status_dict(
            ds=ds,
            status='ok',
            **res_kwargs)
        return annex
