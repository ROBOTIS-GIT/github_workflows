#!/usr/bin/env python3
"""Release existing bloom tracks and guard every GitHub push made by bloom."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

import yaml

VERSION = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
REPOSITORY = r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+'


def run(*args, cwd=None, capture=True, input=None):
    result = subprocess.run(args, cwd=cwd, input=input, text=True, check=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ''


def api(path):
    return json.loads(run('gh', 'api', path))


def validate_inputs(repository, release_repository, version, distros):
    if not re.fullmatch(r'[a-z][a-z0-9_]*', repository):
        raise ValueError('repository must be the rosdistro repository key')
    if not re.fullmatch(REPOSITORY, release_repository):
        raise ValueError('release_repository must be owner/repository')
    if not re.fullmatch(VERSION, version):
        raise ValueError('version must be X.Y.Z without leading zeroes')
    names = distros.split()
    if not names or len(set(names)) != len(names) or any(not re.fullmatch('[a-z][a-z0-9_]*', d) for d in names):
        raise ValueError('distros must contain distinct whitespace-separated distro names')
    return names


def github_repository(url):
    if url.startswith('git@github.com:'):
        path = url.split(':', 1)[1]
    else:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'github.com':
            raise ValueError('Expected a GitHub HTTPS or SSH repository')
        path = parsed.path.lstrip('/')
    path = path.removesuffix('.git').rstrip('/')
    if not re.fullmatch(REPOSITORY, path):
        raise ValueError('Invalid GitHub repository path')
    return path


def pin_track(data, distro, source_repository, version):
    track = data.get('tracks', {}).get(distro)
    if not track:
        raise ValueError(f'Missing track {distro}; onboard it manually first')
    if track.get('ros_distro') != distro or track.get('vcs_type') != 'git':
        raise ValueError(f'Track {distro} must be a matching git-based ROS track')
    if github_repository(track['vcs_uri']).lower() != source_repository.lower():
        raise ValueError(f'Track {distro} upstream does not match the calling repository')
    track.update(version=version, release_tag=version,
                 vcs_uri=f'https://github.com/{source_repository}.git')


def restore_track(data, original, distro):
    # Preserve bloom's last_version/release_inc without locking future manual releases.
    for field in ('version', 'release_tag', 'vcs_uri'):
        data['tracks'][distro][field] = original['tracks'][distro][field]


def release_mode(track, version):
    previous = track.get('last_version')
    if previous:
        if not re.fullmatch(VERSION, previous):
            raise ValueError('Existing track version is not a stable X.Y.Z version')
        if tuple(map(int, previous.split('.'))) > tuple(map(int, version.split('.'))):
            raise ValueError(f'Refusing downgrade from {previous} to {version}')
    return 'pr-only' if previous == version else 'generate'


def released_packages(refs, distro, version, increment):
    prefix = f'refs/tags/release/{distro}/'
    suffix = f'/{version}-{increment}'
    return {ref[len(prefix):-len(suffix)] for ref in refs if ref.startswith(prefix) and ref.endswith(suffix)}


def tag_objects(directory):
    lines = run('git', 'for-each-ref', '--format=%(refname) %(objectname)',
                'refs/tags', cwd=directory).splitlines()
    return dict(line.split() for line in lines)


def import_tag_leases(directory, before, name, version):
    after = tag_objects(directory)
    allowed = [f'refs/tags/upstream/{version}', f'refs/tags/{name}/{version}']
    return [f'--force-with-lease={ref}:{before[ref]}' for ref in dict.fromkeys(allowed)
            if ref in before and after.get(ref) != before[ref]]


def package_data(directory):
    from bloom.packages import get_package_data
    return get_package_data(directory=str(directory), release_directory=str(directory))


def expected_packages(directory, distro, version):
    previous = os.environ.get('BLOOM_TRACK')
    os.environ['BLOOM_TRACK'] = distro
    run('git', 'checkout', 'upstream', cwd=directory)
    try:
        names, actual_version, _ = package_data(directory)
        if actual_version != version:
            raise ValueError(f'{distro}: shared upstream is {actual_version}, expected {version}; '
                             'refusing to create a PR with metadata from another version')
        return set(names)
    finally:
        run('git', 'checkout', 'master', cwd=directory)
        if previous is None:
            os.environ.pop('BLOOM_TRACK', None)
        else:
            os.environ['BLOOM_TRACK'] = previous


def verify_commit(directory, sha):
    run('git', 'verify-commit', sha, cwd=directory)
    message = run('git', 'show', '-s', '--format=%B', sha, cwd=directory)
    trailers = run('git', 'interpret-trailers', '--parse', input=message)
    if not re.search(r'^Signed-off-by: .+ <[^<>\n]+>$', trailers, re.MULTILINE):
        raise ValueError(f'Commit {sha} has no DCO Signed-off-by trailer')


def push_candidates(directory, updates):
    # Existing remote histories are outside this run. FETCH_HEAD covers bloom's
    # PR clone, which fetches ros/rosdistro directly without creating a remote.
    bases = run('git', 'for-each-ref', '--format=%(objectname)', 'refs/remotes', cwd=directory).splitlines()
    fetched = subprocess.run(['git', 'rev-parse', '--verify', 'FETCH_HEAD'], cwd=directory,
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if fetched.returncode == 0:
        bases.append(fetched.stdout.strip())
    if not bases and all(set(update[3]) == {'0'} for update in updates):
        raise ValueError('Cannot verify a push without a fetched baseline')
    tips = []
    for _, local_sha, remote_ref, remote_sha in updates:
        if set(local_sha) == {'0'}:
            raise ValueError('Network ref deletions are not permitted')
        if set(remote_sha) != {'0'}:
            bases.append(remote_sha)
            if remote_ref.startswith('refs/heads/'):
                run('git', 'merge-base', '--is-ancestor', remote_sha, local_sha, cwd=directory)
        tips.append(run('git', 'rev-parse', local_sha + '^{commit}', cwd=directory))
    if not bases:
        raise ValueError('Cannot verify a push without a fetched baseline')
    return run('git', 'rev-list', *tips, '--not', *set(bases), cwd=directory).splitlines() if tips else []


def pre_push(url, stream):
    # bloom's temporary working clones push to local paths. The final network
    # pushes (including its rosdistro PR branch) must cross this boundary.
    if url.startswith('file://') or Path(url).exists():
        return
    repository = github_repository(url)
    allowed = os.environ['BLOOM_ALLOWED_REPOSITORIES'].split()
    if repository.lower() not in {repo.lower() for repo in allowed}:
        raise ValueError('Push to an unconfigured repository is blocked')
    updates = [line.split() for line in stream.splitlines() if line.strip()]
    commits = push_candidates(Path.cwd(), updates)
    for sha in commits:
        verify_commit(Path.cwd(), sha)
    with open(os.environ['BLOOM_PUSH_RECEIPTS'], 'a') as output:
        for sha in commits:
            output.write(json.dumps([repository, sha]) + '\n')


def verify_github_receipts():
    receipts = Path(os.environ['BLOOM_PUSH_RECEIPTS'])
    if not receipts.exists():
        return
    for repository, sha in sorted({tuple(json.loads(line)) for line in receipts.read_text().splitlines()}):
        for attempt in range(5):
            verification = api(f'repos/{repository}/commits/{sha}')['commit']['verification']
            if verification['verified']:
                break
            if attempt == 4:
                raise ValueError(f'GitHub did not verify {repository}@{sha}: {verification["reason"]}')
            time.sleep(2)
    receipts.unlink()


def signoff(message_file):
    name = run('git', 'config', 'user.name')
    email = run('git', 'config', 'user.email')
    run('git', 'interpret-trailers', '--in-place', '--if-exists', 'addIfDifferent',
        '--trailer', f'Signed-off-by: {name} <{email}>', message_file)


def setup_hooks(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for hook, command in [('prepare-commit-msg', 'signoff "$1"'),
                          ('applypatch-msg', 'signoff "$1"'),
                          ('pre-push', 'pre-push "$@"')]:
        path = directory / hook
        path.write_text('#!/bin/sh\nexec python3 "$BLOOM_HELPER" ' + command + '\n')
        path.chmod(0o700)
    run('git', 'config', '--global', 'core.hooksPath', str(directory.resolve()))


def clone_release(url, directory):
    run('git', 'clone', '--branch', 'master', url, str(directory), capture=False)


def commit_tracks(directory, data, message):
    (directory / 'tracks.yaml').write_text(yaml.safe_dump(data, sort_keys=False))
    run('git', 'add', 'tracks.yaml', cwd=directory)
    if run('git', 'diff', '--cached', '--name-only', cwd=directory):
        run('git', 'commit', '-s', '-S', '-m', message, cwd=directory, capture=False)


def open_pr(repository, distro, version, login):
    title = f"{repository}: {version} in '{distro}/distribution.yaml' [bloom]"
    # Avoid GitHub search indexing delays; inspect open PRs directly.
    for page in range(1, 101):
        prs = api(f'repos/ros/rosdistro/pulls?state=open&per_page=100&page={page}')
        for pr in prs:
            if pr['title'] == title and pr['user']['login'].lower() == login.lower():
                return pr['html_url']
        if len(prs) < 100:
            return None
    raise ValueError('Too many open rosdistro PRs to check safely')


def distro_entry(distro, repository):
    content = api(f'repos/ros/rosdistro/contents/{distro}/distribution.yaml')
    import base64
    data = yaml.safe_load(base64.b64decode(content['content']))
    entry = data.get('repositories', {}).get(repository, {}).get('release')
    if not entry:
        raise ValueError(f'{repository} needs an existing release entry in {distro}')
    return entry


def release_one(directory, repository, release_repository, source, version, distro, login):
    url = f'https://github.com/{release_repository}.git'
    clone_release(url, directory)
    original_tags = tag_objects(directory)
    tracks = yaml.safe_load((directory / 'tracks.yaml').read_text())
    original = copy.deepcopy(tracks)
    pin_track(tracks, distro, source, version)
    track = tracks['tracks'][distro]
    mode = release_mode(track, version)
    entry = distro_entry(distro, repository)
    if github_repository(entry['url']).lower() != release_repository.lower():
        raise ValueError(f'{distro} rosdistro entry points at another release repository')
    if mode == 'generate':
        commit_tracks(directory, tracks, f'Pin {distro} release to {version}')
        run('git-bloom-release', distro, '--non-interactive', cwd=directory, capture=False)
        # bloom may leave a release branch checked out; configuration is on master.
        run('git', 'checkout', 'master', cwd=directory)
        tracks = yaml.safe_load((directory / 'tracks.yaml').read_text())
        restore_track(tracks, original, distro)
        commit_tracks(directory, tracks, f'Restore {distro} version detection after {version}')
    track = tracks['tracks'][distro]
    packaged_version = f'{version}-{track["release_inc"]}'
    if mode == 'pr-only' and entry.get('version') == packaged_version:
        return f'Already registered: {packaged_version}'
    refs = run('git', 'for-each-ref', '--format=%(refname)', 'refs/tags', cwd=directory).splitlines()
    packages = released_packages(refs, distro, version, track['release_inc'])
    expected = expected_packages(directory, distro, version)
    if not expected or packages != expected:
        raise ValueError(f'{distro}: incomplete release tags; refusing push or PR')
    if mode == 'generate':
        # One atomic transaction prevents a successful branch push followed by a
        # failed tag push from leaving a release that cannot safely be retried.
        leases = import_tag_leases(directory, original_tags, track['name'], version)
        run('git', 'push', '--atomic', *leases, 'origin', 'refs/heads/*:refs/heads/*',
            'refs/tags/*:refs/tags/*', cwd=directory, capture=False)
        verify_github_receipts()
    if entry.get('version') == packaged_version:
        return f'Already registered: {packaged_version}'
    existing = open_pr(repository, distro, packaged_version, login)
    if existing:
        return f'Existing PR: {existing}'
    # Only the PR phase is delegated to bloom; generation/push were verified above.
    run('bloom-release', repository, '--ros-distro', distro, '--track', distro,
        '--non-interactive', '--pull-request-only', '--no-web',
        '--override-release-repository-url', url, capture=False)
    verify_github_receipts()
    created = open_pr(repository, distro, packaged_version, login)
    if not created:
        raise ValueError(f'{distro}: bloom exited without creating the expected rosdistro PR')
    return f'Created PR: {created}'


def main():
    repository = os.environ['BLOOM_REPOSITORY']
    release_repository = os.environ['BLOOM_RELEASE_REPOSITORY']
    source = os.environ['GITHUB_REPOSITORY']
    version = os.environ['BLOOM_VERSION']
    distros = validate_inputs(repository, release_repository, version, os.environ['BLOOM_DISTROS'])
    release = api(f'repos/{source}/releases/tags/{version}')
    if release['draft'] or release['prerelease']:
        raise ValueError('A published stable GitHub Release is required')
    login = api('user')['login']
    fork = api(f'repos/{login}/rosdistro')
    if fork.get('parent', {}).get('full_name', '').lower() != 'ros/rosdistro':
        raise ValueError('Create a personal fork of ros/rosdistro named rosdistro before running')
    # Set this ourselves; callers cannot redirect a bloom PR push to another repo.
    os.environ['BLOOM_ALLOWED_REPOSITORIES'] = f'{release_repository} {login}/rosdistro'
    config = Path.home() / '.config/bloom'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({'github_user': login, 'oauth_token': os.environ['GH_TOKEN']}))
    config.chmod(0o600)
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix='bloom-release-') as temporary:
            root = Path(temporary)
            # Validate the actual tag manifests before creating any release commits.
            source_dir = root / 'source'
            run('git', 'clone', '--depth', '1', '--branch', version,
                f'https://github.com/{source}.git', str(source_dir), capture=False)
            from catkin_pkg.packages import find_packages
            packages = find_packages(str(source_dir))
            if not packages or any(package.version != version for package in packages.values()):
                raise ValueError('All source package.xml versions must match the release tag')
            for distro in distros:
                print(f'::group::bloom {distro}', flush=True)
                try:
                    result = release_one(root / distro, repository, release_repository,
                                         source, version, distro, login)
                    results.append((distro, True, result))
                except (Exception, SystemExit) as error:
                    # A failed PR phase may already have pushed a signed commit.
                    try:
                        verify_github_receipts()
                    except (ValueError, subprocess.CalledProcessError) as verification_error:
                        error = verification_error
                    message = str(error).replace(os.environ['GH_TOKEN'], '***')
                    results.append((distro, False, message))
                    print(f'::error::{distro}: {message}', flush=True)
                finally:
                    print('::endgroup::', flush=True)
    finally:
        config.unlink(missing_ok=True)
        summary = '\n'.join(f'- {distro}: {"OK" if ok else "FAILED"} — {message}' for distro, ok, message in results)
        print(summary)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
                output.write(f'## Bloom release {version}\n\n{summary}\n')
    if any(not ok for _, ok, _ in results):
        raise SystemExit(1)


if __name__ == '__main__':
    if len(sys.argv) == 1:
        main()
    elif sys.argv[1] == 'setup-hooks':
        setup_hooks(sys.argv[2])
    elif sys.argv[1] == 'signoff':
        signoff(sys.argv[2])
    elif sys.argv[1] == 'pre-push':
        pre_push(sys.argv[3], sys.stdin.read())
    else:
        raise SystemExit('Unknown helper command')
