import os
import re

from rbtools.clients import SCMClient, RepositoryInfo
from rbtools.clients.errors import TooManyRevisionsError
from rbtools.utils.checks import check_install
from rbtools.utils.process import execute


USING_PARENT_PREFIX = 'Using parent branch '


class BazaarClient(SCMClient):
    """
    Bazaar client wrapper that fetches repository information and generates
    compatible diffs.

    The :class:`RepositoryInfo` object reports whether the repository supports
    parent diffs (every branch with a parent supports them).
    """

    name = 'Bazaar'

    # Regular expression that matches the path to the current branch.
    #
    # For branches with shared repositories, Bazaar reports
    # "repository branch: /foo", but for standalone branches it reports
    # "branch root: /foo".
    BRANCH_REGEX = (
        r'\w*(repository branch|branch root|checkout root|checkout of branch):'
        r' (?P<branch_path>.+)$')

    # Revision separator (two ..s without escaping, and not followed by a /).
    # This is the same regex used in bzrlib/option.py:_parse_revision_spec.
    REVISION_SEPARATOR_REGEX = re.compile(r'\.\.(?![\\/])')

    def get_repository_info(self):
        """
        Find out information about the current Bazaar branch (if any) and
        return it.
        """
        if not check_install(['bzr', 'help']):
            return None

        bzr_info = execute(["bzr", "info"], ignore_errors=True)

        if "ERROR: Not a branch:" in bzr_info:
            # This is not a branch:
            repository_info = None
        else:
            # This is a branch, let's get its attributes:
            branch_match = re.search(self.BRANCH_REGEX, bzr_info, re.MULTILINE)

            path = branch_match.group("branch_path")
            if path == ".":
                path = os.getcwd()

            repository_info = RepositoryInfo(
                path=path,
                base_path="/",    # Diffs are always relative to the root.
                supports_parent_diffs=True)

        return repository_info

    def parse_revision_spec(self, revisions=[]):
        """Parses the given revision spec.

        The 'revisions' argument is a list of revisions as specified by the
        user. Items in the list do not necessarily represent a single revision,
        since the user can use SCM-native syntaxes such as "r1..r2" or "r1:r2".
        SCMTool-specific overrides of this method are expected to deal with
        such syntaxes.

        This will return a dictionary with the following keys:
            'base':        A revision to use as the base of the resulting diff.
            'tip':         A revision to use as the tip of the resulting diff.
            'parent_base': (optional) The revision to use as the base of a
                           parent diff.

        These will be used to generate the diffs to upload to Review Board (or
        print). The diff for review will include the changes in (base, tip],
        and the parent diff (if necessary) will include (parent, base].

        If a single revision is passed in, this will return the parent of that
        revision for 'base' and the passed-in revision for 'tip'.

        If zero revisions are passed in, this will return the current HEAD as
        'tip', and the upstream branch as 'base', taking into account parent
        branches explicitly specified via --parent.
        """
        n_revs = len(revisions)
        result = {}

        if n_revs == 0:
            # No revisions were passed in--start with HEAD, and find the
            # submit branch automatically.
            result['tip'] = self._get_revno()
            result['base'] = self._get_revno('ancestor:')
        elif n_revs == 1 or n_revs == 2:
            # If there's a single argument, try splitting it on '..'
            if n_revs == 1:
                revisions = self.REVISION_SEPARATOR_REGEX.split(revisions[0])
                n_revs = len(revisions)

            if n_revs == 1:
                # Single revision. Extract the parent of that revision to use
                # as the base.
                result['base'] = self._get_revno('before:' + revisions[0])
                result['tip'] = self._get_revno(revisions[0])
            elif n_revs == 2:
                # Two revisions.
                result['base'] = self._get_revno(revisions[0])
                result['tip'] = self._get_revno(revisions[1])
            else:
                raise TooManyRevisionsError

            # XXX: I tried to automatically find the parent diff revision here,
            # but I really don't understand the difference between submit
            # branch, parent branch, bound branches, etc. If there's some way
            # to know what to diff against, we could use
            #     'bzr missing --mine-only --my-revision=(base) --line'
            # to see if we need a parent diff.
        else:
            raise TooManyRevisionsError

        if self.options.parent_branch:
            result['parent_base'] = result['base']
            result['base'] = self._get_revno(
                'ancestor:%s' % self.options.parent_branch)

        return result

    def _get_revno(self, revision_spec=None):
        command = ['bzr', 'revno']
        if revision_spec:
            command += ['-r', revision_spec]

        result = execute(command).strip().split('\n')

        if len(result) == 1:
            return 'revno:' + result[0]
        elif len(result) == 2 and result[0].startswith(USING_PARENT_PREFIX):
            branch = result[0][len(USING_PARENT_PREFIX):]
            return 'revno:%s:%s' % (result[1], branch)

    def diff(self, files):
        """Returns the diff of this branch with respect to its parent.

        Additionally sets the summary and description as required.
        """
        files = files or []

        revisions = self.parse_revision_spec()

        rev_log = '%s..%s' % (revisions['base'], revisions['tip'])
        self._set_summary(rev_log)
        self._set_description(rev_log)

        return self._get_diff(revisions, files)

    def diff_between_revisions(self, revision_range, files, repository_info):
        """Returns the diff for the provided revision range.

        The diff is generated for the two revisions in the provided revision
        range. The summary and description are set as required.
        """

        revisions = self.parse_revision_spec(revision_range)

        rev_log = '%s..%s' % (revisions['base'], revisions['tip'])
        self._set_summary(rev_log)
        self._set_description(rev_log)

        return self._get_diff(revisions, files)

    def _get_diff(self, revisions, files):
        diff = self._get_range_diff(revisions['base'], revisions['tip'], files)

        if 'parent_base' in revisions:
            parent_diff = self._get_range_diff(
                revisions['parent_base'], revisions['base'], files)
        else:
            parent_diff = None

        return {
            'diff': diff,
            'parent_diff': parent_diff,
        }

    def _get_range_diff(self, base, tip, files):
        """Return the diff between 'base' and 'tip'."""
        diff_cmd = ['bzr', 'diff', '-q', '-r',
                    '%s..%s' % (base, tip)] + files
        diff = execute(diff_cmd, ignore_errors=True)
        return diff or None

    def _set_summary(self, revision_range=None):
        """Set the summary based on the ``revision_range``.

        Extracts and sets the summary if guessing is enabled and summary is not
        yet set.
        """
        if self.options.guess_summary and not self.options.summary:
            self.options.summary = self.extract_summary(revision_range)

    def _set_description(self, revision_range=None):
        """Set the description based on the ``revision_range``.

        Extracts and sets the description if guessing is enabled and
        description is not yet set.
        """
        if self.options.guess_description and not self.options.description:
            self.options.description = self.extract_description(revision_range)

    def extract_summary(self, revision_range=None):
        """Return the last commit message in ``revision_range``.

        If revision_range is ``None``, the commit message of the last revision
        in the repository is returned.
        """
        if revision_range:
            revision = revision_range.split("..")[1]
        else:
            revision = '-1'

        # `bzr log --line' returns the log in the format:
        #   {revision-number}: {committer-name} {commit-date} {commit-message}
        # So we should ignore everything after the date (YYYY-MM-DD).
        log_message = execute(
            ["bzr", "log", "-r", revision, "--line"]).rstrip()
        log_message_match = re.search(r"\d{4}-\d{2}-\d{2}", log_message)
        truncated_characters = log_message_match.end() + 1

        summary = log_message[truncated_characters:]

        return summary

    def extract_description(self, revision_range=None):
        command = ['bzr', 'log', '-r', revision_range, '--short']
        return execute(command, ignore_errors=True).rstrip()
