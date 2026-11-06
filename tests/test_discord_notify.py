"""Run with: python3 -m unittest discover -s tests -v (requires PyYAML and jq)."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/discord-notify.yml'


class DiscordNotifyTest(unittest.TestCase):
    def test_payload_and_failure_handling(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        script = workflow['jobs']['notify']['steps'][0]['run']
        subprocess.run(['bash', '-n'], input=script, text=True, check=True)
        # Exercise the actual jq filter embedded in the workflow.
        query = script.split("| jq --arg repository \"$GH_REPO\" '", 1)[1].split("' >", 1)[0]
        for body in ['## Changes\nFixed "quotes" and @everyone', '', None, '가😀' * 5000]:
            with self.subTest(body_length=len(body or '')):
                release = {'name': '1.2.3', 'body': body, 'url': 'https://github.com/org/repo/releases/tag/1.2.3'}
                result = subprocess.run(
                    ['jq', '--arg', 'repository', 'org/repo', query],
                    input=json.dumps(release), text=True, capture_output=True, check=True,
                )
                payload = json.loads(result.stdout)
                embed = payload['embeds'][0]
                self.assertEqual(payload['allowed_mentions'], {'parse': []})
                self.assertIn('org/repo', embed['title'])
                self.assertIn('1.2.3', embed['title'])
                self.assertEqual(embed['url'], release['url'])
                self.assertLessEqual(len(embed['title'].encode('utf-16-le')) // 2, 256)
                self.assertLessEqual(len(embed['description'].encode('utf-16-le')) // 2, 4096)
                if body and len(body) < 1900:
                    self.assertEqual(embed['description'], body)
                if body and len(body) > 1900:
                    self.assertIn('release link', embed['description'])
        self.assertIn('set -euo pipefail', script)
        self.assertIn('--fail-with-body', script)
        self.assertIn('wait=true', script)

    def test_delivery_and_errors(self):
        script = yaml.safe_load(WORKFLOW.read_text())['jobs']['notify']['steps'][0]['run']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Replace only the external services; execute the real workflow shell.
            gh = root / 'gh'
            gh.write_text('#!/bin/bash\ncat "$RUNNER_TEMP/release.json"\n')
            curl = root / 'curl'
            curl.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$RUNNER_TEMP/curl-args"\nexit "${CURL_STATUS:-0}"\n')
            gh.chmod(0o755)
            curl.chmod(0o755)
            env = {
                **os.environ, 'PATH': directory + ':' + os.environ['PATH'],
                'RUNNER_TEMP': directory, 'GH_REPO': 'org/repo', 'VERSION': '1.2.3',
                'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/test/token',
            }
            release = {'name': '1.2.3', 'tagName': '1.2.3', 'body': 'Changes',
                       'url': 'https://github.com/org/repo/releases/tag/1.2.3', 'isDraft': False}
            release_file = root / 'release.json'
            release_file.write_text(json.dumps(release))
            result = subprocess.run(['bash', '-c', script], env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(env['DISCORD_WEBHOOK_URL'] + '?wait=true', (root / 'curl-args').read_text())
            payload = json.loads((root / 'discord-message.json').read_text())
            self.assertEqual(payload['embeds'][0]['description'], 'Changes')

            env['DISCORD_WEBHOOK_URL'] += '?thread_id=123'
            result = subprocess.run(['bash', '-c', script], env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('?thread_id=123&wait=true', (root / 'curl-args').read_text())

            env['CURL_STATUS'] = '22'
            result = subprocess.run(['bash', '-c', script], env=env, capture_output=True)
            self.assertEqual(result.returncode, 22)

            for reason in ['draft', 'missing-secret']:
                with self.subTest(reason=reason):
                    (root / 'curl-args').unlink(missing_ok=True)
                    release['isDraft'] = reason == 'draft'
                    release_file.write_text(json.dumps(release))
                    case_env = {**env, 'CURL_STATUS': '0'}
                    if reason == 'missing-secret':
                        case_env['DISCORD_WEBHOOK_URL'] = ''
                    result = subprocess.run(['bash', '-c', script], env=case_env, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((root / 'curl-args').exists())


if __name__ == '__main__':
    unittest.main()
