"""Offline checks for bloom planning and the signed push boundary."""
import copy
from contextlib import redirect_stdout
import io
import os
import sys
from types import ModuleType, SimpleNamespace
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bloom_release', ROOT / 'scripts/bloom_release.py')
bloom = importlib.util.module_from_spec(spec)
if spec.loader.path and Path(spec.loader.path).exists():
    spec.loader.exec_module(bloom)


class BloomReleaseTest(unittest.TestCase):
    def test_rejects_invalid_inputs(self):
        for version in ['v4.1.0', '4.01.0', '4.1.0;touch /tmp/oops', '', '4.1.0-rc1']:
            with self.subTest(version=version), self.assertRaises(ValueError):
                bloom.validate_inputs('dynamixel_sdk', 'org/sdk-release', version, 'humble rolling')
        for distros in ['', 'humble humble', 'humble;echo', '../humble']:
            with self.subTest(distros=distros), self.assertRaises(ValueError):
                bloom.validate_inputs('dynamixel_sdk', 'org/sdk-release', '4.1.0', distros)
        self.assertEqual(bloom.validate_inputs('dynamixel_sdk', 'org/sdk-release', '4.1.0', 'humble\nrolling'), ['humble', 'rolling'])

    def test_track_is_pinned_without_changing_other_distros(self):
        tracks = {'tracks': {'humble': {'ros_distro': 'humble', 'vcs_type': 'git',
                  'vcs_uri': 'https://github.com/org/sdk.git', 'version': ':{auto}',
                  'release_tag': ':{version}', 'devel_branch': 'humble', 'last_version': '4.0.3',
                  'release_inc': '2'}, 'rolling': {'version': ':{auto}'}}}
        original = copy.deepcopy(tracks)
        bloom.pin_track(tracks, 'humble', 'org/sdk', '4.1.0')
        self.assertEqual(tracks['tracks']['humble']['version'], '4.1.0')
        self.assertEqual(tracks['tracks']['humble']['release_tag'], '4.1.0')
        self.assertEqual(tracks['tracks']['rolling'], original['tracks']['rolling'])
        tracks['tracks']['humble'].update(last_version='4.1.0', release_inc='1')
        bloom.restore_track(tracks, original, 'humble')
        self.assertEqual(tracks['tracks']['humble']['version'], ':{auto}')
        self.assertEqual(tracks['tracks']['humble']['last_version'], '4.1.0')
        self.assertEqual(tracks['tracks']['humble']['release_inc'], '1')
        for changed in [{'vcs_uri': 'https://github.com/other/sdk.git'}, {'ros_distro': 'jazzy'}, {'vcs_type': 'hg'}]:
            data = copy.deepcopy(original)
            data['tracks']['humble'].update(changed)
            with self.assertRaises(ValueError):
                bloom.pin_track(data, 'humble', 'org/sdk', '4.1.0')
        with self.assertRaises(ValueError):
            bloom.pin_track(original, 'lyrical', 'org/sdk', '4.1.0')

    def test_retry_does_not_increment_and_downgrade_fails(self):
        self.assertEqual(bloom.release_mode({'last_version': '4.0.3', 'release_inc': '1'}, '4.1.0'), 'generate')
        self.assertEqual(bloom.release_mode({'last_version': '4.1.0', 'release_inc': '1'}, '4.1.0'), 'pr-only')
        with self.assertRaises(ValueError):
            bloom.release_mode({'last_version': '4.2.0', 'release_inc': '1'}, '4.1.0')

    def test_unsigned_or_missing_dco_is_rejected(self):
        # Real Git parses a synthetic unsigned fixture; no unsigned git commit is made.
        with tempfile.TemporaryDirectory() as directory:
            def git(*args, **kwargs):
                return subprocess.check_output(['git', '-C', directory, *args], text=True, **kwargs).strip()
            git('init', '-q')
            tree = git('mktree', input='')
            raw = f'tree {tree}\nauthor Fixture <fixture@example.test> 1 +0000\ncommitter Fixture <fixture@example.test> 1 +0000\n\nfixture\n'
            sha = git('hash-object', '-w', '-t', 'commit', '--stdin', input=raw)
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                bloom.verify_commit(Path(directory), sha)
            # A valid signature alone must never bypass the DCO check.
            with patch.object(bloom, 'run', return_value='fixture\n'):
                with self.assertRaises(ValueError):
                    bloom.verify_commit(Path(directory), sha)

    def test_github_remote_parsing_never_logs_credentials(self):
        for url in ['https://github.com/org/repo.git', 'git@github.com:org/repo.git',
                    'https://secret:x-oauth-basic@github.com/org/repo.git']:
            self.assertEqual(bloom.github_repository(url), 'org/repo')
        for url in ['https://evil.example/org/repo.git', '/tmp/local']:
            with self.assertRaises(ValueError):
                bloom.github_repository(url)

    def test_network_push_with_no_baseline_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['git', 'init', '-q', directory], check=True)
            with self.assertRaises(ValueError):
                bloom.push_candidates(Path(directory), [('refs/heads/main', 'a' * 40, 'refs/heads/main', '0' * 40)])

    def test_push_candidates_excludes_existing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args, **kwargs):
                return subprocess.check_output(['git', '-C', directory, *args], text=True, **kwargs).strip()
            git('init', '-q')
            tree = git('mktree', input='')
            def fixture(parent=''):
                raw = f'tree {tree}\n{parent}author Fixture <fixture@example.test> 1 +0000\ncommitter Fixture <fixture@example.test> 1 +0000\n\nfixture\n'
                return git('hash-object', '-w', '-t', 'commit', '--stdin', input=raw)
            base = fixture()
            tip = fixture(f'parent {base}\n')
            git('update-ref', 'refs/remotes/origin/main', base)
            updates = [('refs/heads/main', tip, 'refs/heads/main', base)]
            self.assertEqual(bloom.push_candidates(root, updates), [tip])
            updates = [('refs/heads/new', tip, 'refs/heads/new', '0' * 40)]
            self.assertEqual(bloom.push_candidates(root, updates), [tip])
            with self.assertRaises(ValueError):
                bloom.push_candidates(root, [('refs/heads/main', '0' * 40, 'refs/heads/main', base)])

    def test_signoff_preserves_existing_trailers(self):
        with tempfile.TemporaryDirectory() as directory:
            message = Path(directory) / 'message'
            message.write_text('Release\n\nSigned-off-by: Original <original@example.test>\n')
            env = {'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': 'user.name',
                   'GIT_CONFIG_VALUE_0': 'Fixture', 'GIT_CONFIG_KEY_1': 'user.email',
                   'GIT_CONFIG_VALUE_1': 'fixture@example.test'}
            with patch.dict(os.environ, env):
                bloom.signoff(str(message))
                bloom.signoff(str(message))
            self.assertIn('Signed-off-by: Original <original@example.test>', message.read_text())
            self.assertEqual(message.read_text().count('Signed-off-by: Fixture <fixture@example.test>'), 1)

    def test_push_guard_blocks_unknown_network_and_checks_before_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'receipts'
            env = {'BLOOM_ALLOWED_REPOSITORIES': 'org/release', 'BLOOM_PUSH_RECEIPTS': str(receipt)}
            with patch.dict(os.environ, env):
                with self.assertRaises(ValueError):
                    bloom.pre_push('https://github.com/other/repo.git', '')
                with patch.object(bloom, 'push_candidates', return_value=['a' * 40]), \
                     patch.object(bloom, 'verify_commit', side_effect=ValueError('unsigned')):
                    with self.assertRaises(ValueError):
                        bloom.pre_push('https://github.com/org/release.git', '')
                self.assertFalse(receipt.exists())

    def test_only_target_release_tags_count(self):
        refs = ['refs/tags/release/humble/sdk/4.1.0-1', 'refs/tags/release/rolling/sdk/4.1.0-1',
                'refs/tags/release/humble/sdk/4.1.0-10']
        self.assertEqual(bloom.released_packages(refs, 'humble', '4.1.0', '1'), {'sdk'})
        self.assertEqual(bloom.released_packages(refs, 'humble', '4.1.0', '2'), set())

    def test_import_tag_leases_are_narrow(self):
        before = {'refs/tags/upstream/4.1.0': 'a' * 40,
                  'refs/tags/sdk/4.1.0': 'b' * 40,
                  'refs/tags/release/humble/sdk/4.1.0-1': 'c' * 40}
        after = {key: 'd' * 40 for key in before}
        with patch.object(bloom, 'tag_objects', return_value=after):
            leases = bloom.import_tag_leases(Path('.'), before, 'sdk', '4.1.0')
        self.assertEqual(leases, ['--force-with-lease=refs/tags/upstream/4.1.0:' + 'a' * 40,
                                  '--force-with-lease=refs/tags/sdk/4.1.0:' + 'b' * 40])

    def test_retry_rejects_upstream_metadata_from_another_version(self):
        # Wrong shared upstream metadata must not reach the PR-only bloom command.
        with patch.object(bloom, 'run', return_value=''), \
             patch.object(bloom, 'package_data', return_value=(['sdk'], '4.2.0', {})):
            with self.assertRaises(ValueError):
                bloom.expected_packages(Path('.'), 'humble', '4.1.0')
        with patch.object(bloom, 'run', return_value=''), \
             patch.object(bloom, 'package_data', return_value=(['renamed_sdk'], '4.1.0', {})):
            self.assertEqual(bloom.expected_packages(Path('.'), 'humble', '4.1.0'), {'renamed_sdk'})

    def test_registered_retry_neither_regenerates_nor_pushes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracks = {'tracks': {'humble': {'ros_distro': 'humble', 'vcs_type': 'git',
                      'vcs_uri': 'git@github.com:org/sdk.git', 'version': ':{auto}',
                      'release_tag': ':{version}', 'last_version': '4.1.0', 'release_inc': '1'}}}
            def clone(url, directory):
                directory.mkdir()
                (directory / 'tracks.yaml').write_text(yaml.safe_dump(tracks))
            with patch.object(bloom, 'clone_release', side_effect=clone), \
                 patch.object(bloom, 'tag_objects', return_value={}), \
                 patch.object(bloom, 'distro_entry', return_value={'url': 'https://github.com/org/sdk-release.git', 'version': '4.1.0-1'}), \
                 patch.object(bloom, 'run') as commands:
                result = bloom.release_one(root / 'release', 'sdk', 'org/sdk-release', 'org/sdk', '4.1.0', 'humble', 'user')
            self.assertIn('Already registered', result)
            commands.assert_not_called()
            bloom.pin_track(tracks, 'humble', 'org/sdk', '4.1.0')
            self.assertEqual(tracks['tracks']['humble']['vcs_uri'], 'https://github.com/org/sdk.git')

    def test_distro_failure_still_runs_remaining_tracks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {'BLOOM_REPOSITORY': 'sdk', 'BLOOM_RELEASE_REPOSITORY': 'org/sdk-release',
                   'GITHUB_REPOSITORY': 'org/sdk', 'BLOOM_VERSION': '4.1.0',
                   'BLOOM_DISTROS': 'humble jazzy', 'GH_TOKEN': 'fixture-token',
                   'GITHUB_STEP_SUMMARY': str(root / 'summary')}
            def response(path):
                if path == 'user':
                    return {'login': 'fixture'}
                if path == 'repos/fixture/rosdistro':
                    return {'parent': {'full_name': 'ros/rosdistro'}}
                return {'draft': False, 'prerelease': False}
            fake_packages = SimpleNamespace(find_packages=lambda path: {'sdk': SimpleNamespace(version='4.1.0')})
            with patch.dict(os.environ, env), patch.dict(sys.modules, {'catkin_pkg': ModuleType('catkin_pkg'), 'catkin_pkg.packages': fake_packages}), \
                 patch.object(Path, 'home', return_value=root), \
                 patch.object(bloom, 'api', side_effect=response), patch.object(bloom, 'run'), \
                 patch.object(bloom, 'verify_github_receipts'), \
                 patch.object(bloom, 'release_one', side_effect=[SystemExit(2), 'PR created']) as release:
                with self.assertRaises(SystemExit) as failure, redirect_stdout(io.StringIO()):
                    bloom.main()
            self.assertEqual(failure.exception.code, 1)
            self.assertEqual(release.call_count, 2)
            self.assertIn('humble: FAILED', (root / 'summary').read_text())
            self.assertIn('jazzy: OK', (root / 'summary').read_text())
            self.assertFalse((root / '.config/bloom').exists())

    def test_workflow_contract_and_shell_syntax(self):
        workflow = yaml.load((ROOT / '.github/workflows/bloom-release.yml').read_text(), Loader=yaml.BaseLoader)
        call = workflow['on']['workflow_call']
        self.assertEqual(call['inputs']['distros']['default'], 'humble jazzy kilted lyrical rolling')
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(workflow['jobs']['bloom']['concurrency']['cancel-in-progress'], 'false')
        for step in workflow['jobs']['bloom']['steps']:
            if 'run' in step:
                subprocess.run(['bash', '-n'], input=step['run'], text=True, check=True)


if __name__ == '__main__':
    unittest.main()
