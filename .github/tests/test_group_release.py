import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / '.github/workflows/sync-baseline.yml').read_text())
RUN = next(step['run'] for step in WORKFLOW['jobs']['coordinate']['steps'] if step.get('id') == 'coordinate')
CODE = RUN.split("<<'PY'", 1)[1].split('\n', 1)[1].rsplit('\nPY', 1)[0]


class GitHub:
    repo = 'linuxdeepin/dde'

    def __init__(self):
        self.cfg = {'groups': {
            'G1': {'version': '6.7.52', 'members': ['dtkcore']},
            'G2': {'version': '6.0.49', 'members': ['go-lib', 'dde-api']},
            'G3': {'version': '6.1.113', 'members': ['dde-daemon', 'dde-clipboard']},
            'G5': {'version': '2026.02.27', 'members': ['deepin-icon-theme']},
        }}
        self.configs = {'base': copy.deepcopy(self.cfg)}
        self.commits = {'base': {'sha': 'base', 'tree': {'sha': 'tree-base'}, 'parents': [], 'message': 'base'}}
        self.refs = {'master': 'base'}
        self.prs = []
        self.member_prs = {}
        self.versions = {}
        self.live = {f'linuxdeepin/{m}': g['version'] for g in self.cfg['groups'].values() for m in g['members']}
        self.runs = {}
        self.workflow_info = {}
        self.workflow_files = {}
        self.writes = []
        self.statuses = {}
        self.serial = 0
        self.fail_ref = False
        self.fail_pr = False
        self.ns = {'__name__': 'group_release_test'}
        exec(compile(CODE, 'sync-baseline.py', 'exec'), self.ns)
        self.ns.update(api=self.api, config=self.config, content=self.content, head_version=self.head_version)

    def config(self, repo, sha):
        return copy.deepcopy(self.configs[sha])

    def content(self, repo, path, sha):
        if path.startswith('.github/workflows/'):
            text = self.workflow_files.get((repo, path, sha), 'on:\n  workflow_dispatch:\n')
            if text is None:
                raise RuntimeError(f'{repo}@{sha} 缺少成员工作流 (HTTP 404)')
            return text
        return yaml.safe_dump(self.configs[sha], sort_keys=False)

    def head_version(self, repo, ref):
        return self.versions.get((repo, ref), self.live[repo])

    def api(self, url, data=None, method=None, missing=False, paginate=False):
        path = urlsplit(url).path
        query = parse_qs(urlsplit(url).query)
        if data is not None or method:
            self.writes.append((url, data, method))
        if path == f'repos/{self.repo}/pulls':
            if data:
                if self.fail_pr:
                    raise RuntimeError('PR API unavailable')
                number = len(self.prs) + 1
                pr = {**data, 'number': number, 'state': 'open', 'merged_at': None, 'node_id': str(number),
                      'head': {'ref': data['head'], 'sha': self.refs[data['head']], 'repo': {'full_name': self.repo}},
                      'base': {'ref': data['base'], 'sha': self.refs['master']}, 'created_at': '9999-01-01T00:00:00Z'}
                self.prs.insert(0, pr)
                return copy.deepcopy(pr)
            prs = self.prs
            if 'head' in query:
                branch = query['head'][0].split(':', 1)[1]
                prs = [p for p in prs if p['head']['ref'] == branch]
            if query.get('state') == ['open']:
                prs = [p for p in prs if p['state'] == 'open']
            if query.get('per_page') == ['1']:
                prs = prs[:1]
            return copy.deepcopy(prs)
        if path.startswith(f'repos/{self.repo}/pulls/'):
            number = int(path.rsplit('/', 1)[1])
            pr = next(p for p in self.prs if p['number'] == number)
            if data:
                pr.update(data)
            return copy.deepcopy(pr)
        if path.startswith(f'repos/{self.repo}/git/ref/heads/'):
            branch = path.split('/heads/', 1)[1]
            return {'object': {'sha': self.refs[branch]}} if branch in self.refs else None
        if path.startswith(f'repos/{self.repo}/git/refs/heads/') and method == 'DELETE':
            del self.refs[path.split('/heads/', 1)[1]]
            return None
        if path == f'repos/{self.repo}/git/refs':
            branch = data['ref'].removeprefix('refs/heads/')
            if self.fail_ref or branch in self.refs:
                raise RuntimeError('reference already exists')
            self.refs[branch] = data['sha']
            return {}
        if path == f'repos/{self.repo}/git/blobs':
            self.blob = yaml.safe_load(data['content'])
            return {'sha': 'blob'}
        if path == f'repos/{self.repo}/git/trees':
            return {'sha': 'tree'}
        if path == f'repos/{self.repo}/git/commits':
            self.serial += 1
            sha = f'commit-{self.serial}'
            self.commits[sha] = {'sha': sha, 'message': data['message'], 'parents': [{'sha': p} for p in data['parents']], 'tree': {'sha': 'tree'}}
            self.configs[sha] = copy.deepcopy(self.blob)
            return {'sha': sha}
        if path.startswith(f'repos/{self.repo}/git/commits/'):
            return self.commits[path.rsplit('/', 1)[1]]
        if path.startswith(f'repos/{self.repo}/commits/') and path.endswith('/statuses'):
            return copy.deepcopy(self.statuses.get(path.split('/')[-2], []))
        if path.startswith(f'repos/{self.repo}/statuses/'):
            self.statuses.setdefault(path.rsplit('/', 1)[1], []).insert(0, copy.deepcopy(data))
            return {}
        if path.startswith(f'repos/{self.repo}/compare/'):
            base, head = path.rsplit('/', 1)[1].split('...')
            def ancestors(sha):
                result = [sha]
                for parent in self.commits[sha]['parents']:
                    result.extend(ancestors(parent['sha']))
                return result
            base_ancestors, head_ancestors = ancestors(base), ancestors(head)
            common = base if base in head_ancestors else next(s for s in head_ancestors if s in base_ancestors)
            files = [] if self.configs[common] == self.configs[head] else [{'filename': 'groups.yaml', 'status': 'modified'}]
            return {'merge_base_commit': {'sha': common}, 'files': files}
        if path == 'graphql':
            next(p for p in self.prs if p['node_id'] == data['variables']['id'])['draft'] = False
            return {}
        if path.endswith('/pulls'):
            repo = path.removeprefix('repos/').removesuffix('/pulls')
            prs = self.member_prs.get(repo, [])
            if 'head' in query:
                branch = query['head'][0].split(':', 1)[1]
                prs = [p for p in prs if p['head']['ref'] == branch]
            if 'base' in query:
                prs = [p for p in prs if p.get('base', {}).get('ref', 'master') == query['base'][0]]
            if query.get('state') == ['open']:
                prs = [p for p in prs if p['state'] == 'open']
            return copy.deepcopy(prs)
        if '/actions/workflows/' in path:
            repo, workflow = path.removeprefix('repos/').split('/actions/workflows/', 1)
            if workflow.endswith('/runs'):
                return {'workflow_runs': self.runs.get(repo, [])}
            info = self.workflow_info.get((repo, workflow), {'state': 'active'})
            if info is None:
                raise RuntimeError(f'{repo} 缺少成员工作流 (HTTP 404)')
            return copy.deepcopy(info)
        if path.startswith('repos/') and path.count('/') == 2:
            return {'default_branch': 'master'}
        raise AssertionError((url, data, method))

    def start(self, group='G2', version='6.0.50', dry_run=False, **options):
        return self.ns['start'](self.repo, 'master', self.refs['master'], copy.deepcopy(self.cfg), group, version, dry_run, **options)

    def release(self, group='G2'):
        pr = next(p for p in self.prs if p['head']['ref'] == f'baseline/{group}')
        return self.ns['record'](self.repo, pr['head']['sha'], group)

    def member_pr(self, repo, merged=False, group='G2', pending=True):
        release = self.release(group)
        member = next(m for m in release['members'] if m['repo'] == repo)
        base = member['base']
        target = member.get('version', release['version'])
        branch = self.ns['member_branch'](group, target)
        pr = {'number': 1, 'html_url': f'https://github.com/{repo}/pull/1',
              'created_at': '9999-01-01T00:00:00Z', 'state': 'closed' if merged or not pending else 'open',
              'merged_at': '9999-01-02T00:00:00Z' if merged else None, 'merge_commit_sha': 'member-merge',
              'head': {'ref': branch, 'sha': 'member-head', 'repo': {'full_name': repo}}, 'base': {'ref': base}}
        self.member_prs[repo] = [pr]
        pr['head']['sha'] = f"member-head-{target}"
        pr['merge_commit_sha'] = f"member-merge-{target}"
        self.versions[(repo, pr['head']['sha'])] = target
        self.versions[(repo, pr['merge_commit_sha'])] = target
        if merged:
            if base == 'master':
                self.live[repo] = target
            else:
                self.versions[(repo, base)] = target
        return pr

    def merge(self, group='G2'):
        pr = next(p for p in self.prs if p['head']['ref'] == f'baseline/{group}')
        pr.update(state='closed', merged_at='9999-01-03T00:00:00Z')
        self.cfg['groups'][group]['version'] = self.release(group)['version']
        sha = f'merged-{pr["number"]}'
        old_base = self.refs['master']
        self.refs['master'] = sha
        pr['merge_commit_sha'] = sha
        self.configs[sha] = copy.deepcopy(self.cfg)
        self.commits[sha] = {'sha': sha, 'message': 'Merge baseline PR', 'tree': {'sha': 'base-tree'},
                             'parents': [{'sha': old_base}, {'sha': pr['head']['sha']}]}
        self.refs.pop(f'baseline/{group}', None)


class GroupReleaseTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, GITHUB_SERVER_URL='https://github.com', GITHUB_RUN_ID='123')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.gh = GitHub()

    def test_fresh_release_reserves_before_pr_and_returns_all_members(self):
        members = self.gh.start()
        self.assertEqual(len(members), 2)
        paths = [p for p, _, _ in self.gh.writes]
        self.assertLess(paths.index('repos/linuxdeepin/dde/git/refs'), paths.index('repos/linuxdeepin/dde/pulls'))
        self.assertTrue(self.gh.prs[0]['draft'])
        self.assertIn('### 成员发布 PR', self.gh.prs[0]['body'])
        self.assertEqual(self.gh.prs[0]['body'].count('尚无发布 PR'), 2)
        self.assertEqual(self.gh.cfg['groups']['G2']['version'], '6.0.49')
        self.assertEqual(self.gh.configs[self.gh.prs[0]['head']['sha']]['groups']['G2']['version'], '6.0.50')

    def test_dry_run_does_not_write(self):
        self.assertEqual(len(self.gh.start(dry_run=True)), 2)
        self.assertEqual(self.gh.writes, [])

    def test_changelog_without_pr_does_not_reserve_a_release(self):
        self.assertEqual(len(self.gh.start(create_pr_enabled=False)), 2)
        self.assertEqual(self.gh.writes, [])
        self.assertNotIn('baseline/G2', self.gh.refs)
        self.assertEqual(len(self.gh.start()), 2)

    def test_selected_member_branch_is_used_for_checks_and_resume(self):
        for repo in ('linuxdeepin/go-lib', 'linuxdeepin/dde-api'):
            self.gh.live[repo] = '6.0.99'
            self.gh.versions[(repo, 'release/stable')] = '6.0.49'
        members = self.gh.start(member_base='release/stable')
        self.assertEqual({m['base'] for m in members}, {'release/stable'})
        self.assertEqual({m['default_branch'] for m in members}, {'master'})
        self.gh.member_pr('linuxdeepin/go-lib', merged=True)
        self.gh.member_pr('linuxdeepin/dde-api')
        self.assertEqual(self.gh.start(), [])
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertTrue(self.gh.prs[0]['draft'])
        self.gh.member_pr('linuxdeepin/dde-api', merged=True)
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertFalse(self.gh.prs[0]['draft'])

    def test_resume_cannot_change_member_branch(self):
        self.gh.start(member_base='release/stable')
        before = len(self.gh.writes)
        with self.assertRaisesRegex(RuntimeError, '不能改变成员目标分支'):
            self.gh.start(member_base='master')
        self.assertEqual(before, len(self.gh.writes))
        self.assertEqual({m['base'] for m in self.gh.start()}, {'release/stable'})

    def test_resume_cannot_change_distribution(self):
        self.gh.start(distribution='stable')
        self.assertEqual(self.gh.release()['distribution'], 'stable')
        self.gh.member_pr('linuxdeepin/go-lib', merged=True)
        before = len(self.gh.writes)
        with self.assertRaisesRegex(RuntimeError, '不能改变发行套件'):
            self.gh.start(distribution='unstable')
        self.assertEqual(before, len(self.gh.writes))
        self.assertEqual([m['repo'] for m in self.gh.start(distribution='stable')],
                         ['linuxdeepin/dde-api'])

    def test_selected_branch_target_must_exceed_its_current_version(self):
        self.gh.versions[('linuxdeepin/go-lib', 'release/stable')] = '6.0.50'
        with self.assertRaisesRegex(RuntimeError, '实际版本'):
            self.gh.start(member_base='release/stable')
        self.assertEqual(self.gh.writes, [])

    def test_selected_branch_preserves_default_branch_change_guard(self):
        self.gh.start(member_base='release/stable')
        original = self.gh.api
        def changed_default(url, *args, **kwargs):
            if url == 'repos/linuxdeepin/go-lib':
                return {'default_branch': 'new-default'}
            return original(url, *args, **kwargs)
        self.gh.ns['api'] = changed_default
        before = len(self.gh.writes)
        with self.assertRaisesRegex(RuntimeError, '默认分支已改变'):
            self.gh.start()
        self.assertEqual(before, len(self.gh.writes))

    def test_legacy_release_record_without_default_branch_can_resume(self):
        self.gh.start()
        pr = self.gh.prs[0]
        release = self.gh.release()
        for member in release['members']:
            member.pop('default_branch')
        self.gh.commits[pr['head']['sha']]['message'] = self.gh.ns['MARKER'] + json.dumps(release) + '\n-->'
        self.assertEqual(len(self.gh.start()), 2)

    def test_g1_rejects_unsupported_changelog_options_before_reservation(self):
        for options in ({'member_base': 'master'}, {'distribution': 'stable'}, {'create_pr_enabled': False}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(RuntimeError, 'G1.*不支持'):
                    self.gh.start('G1', '6.7.53', **options)
                self.assertEqual(self.gh.writes, [])

    def test_first_release_unifies_mixed_versions_and_resumes(self):
        self.gh.cfg['groups']['G2']['members'].append('go-dbus-factory')
        self.gh.configs['base'] = copy.deepcopy(self.gh.cfg)
        self.gh.live.update({'linuxdeepin/go-lib': '6.0.15',
                             'linuxdeepin/go-dbus-factory': '2.0.31'})
        initial = copy.deepcopy(self.gh.live)
        self.assertEqual(len(self.gh.start(dry_run=True)), 3)
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(len(self.gh.start()), 3)
        self.assertEqual(self.gh.live, initial)
        self.gh.member_pr('linuxdeepin/go-lib', merged=True)
        self.assertEqual({m['repo'] for m in self.gh.start()},
                         {'linuxdeepin/dde-api', 'linuxdeepin/go-dbus-factory'})
        with self.assertRaisesRegex(RuntimeError, '正在发布'):
            self.gh.start(version='6.0.51')
        for repo in ('linuxdeepin/dde-api', 'linuxdeepin/go-dbus-factory'):
            self.gh.member_pr(repo, merged=True)
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertFalse(self.gh.prs[0]['draft'])
        self.assertEqual(self.gh.cfg['groups']['G2']['version'], '6.0.49')
        self.gh.merge()
        self.assertEqual(self.gh.cfg['groups']['G2']['version'], '6.0.50')
        self.assertEqual(len(self.gh.start(version='6.0.51')), 3)

    def test_first_target_must_exceed_every_member(self):
        for actual in ('6.0.50', '6.0.51'):
            for dry_run in (True, False):
                with self.subTest(actual=actual, dry_run=dry_run):
                    gh = GitHub()
                    gh.live['linuxdeepin/go-lib'] = actual
                    with self.assertRaisesRegex(RuntimeError, '必须大于.*实际版本'):
                        gh.start(dry_run=dry_run)
                    self.assertEqual(gh.writes, [])

    def test_first_release_can_start_above_an_outdated_baseline(self):
        self.gh.live['linuxdeepin/go-lib'] = '6.0.51'
        self.assertEqual(len(self.gh.start(version='6.0.52', dry_run=True)), 2)
        self.assertEqual(self.gh.writes, [])

    def test_first_release_freezes_each_members_epoch_version(self):
        self.gh.live['linuxdeepin/dde-clipboard'] = '1:6.1.37'
        expected = {'linuxdeepin/dde-daemon': '6.1.114',
                    'linuxdeepin/dde-clipboard': '1:6.1.114'}
        planned = self.gh.start('G3', '6.1.114', dry_run=True)
        self.assertEqual({m['repo']: m['version'] for m in planned}, expected)
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(self.gh.start('G3', '6.1.114'), planned)
        release = self.gh.release('G3')
        self.assertEqual(release['members'], planned)
        self.assertEqual(release['version'], '6.1.114')
        self.assertEqual(self.gh.configs[self.gh.prs[0]['head']['sha']]['groups']['G3']['version'], '6.1.114')

    def test_epoch_release_resumes_and_completes_with_frozen_targets(self):
        clipboard = 'linuxdeepin/dde-clipboard'
        self.gh.live[clipboard] = '1:6.1.37'
        self.gh.start('G3', '6.1.114')
        self.gh.live[clipboard] = '6.1.37'
        resumed = self.gh.start('G3', '6.1.114', dry_run=True)
        self.assertEqual(next(m['version'] for m in resumed if m['repo'] == clipboard), '1:6.1.114')
        self.gh.live[clipboard] = '1:6.1.37'
        self.gh.member_pr('linuxdeepin/dde-daemon', merged=True, group='G3')
        member_pr = self.gh.member_pr(clipboard, group='G3')
        self.assertEqual(member_pr['head']['ref'], 'automation/update-changelog/1-6.1.114')
        self.assertEqual(self.gh.start('G3', '6.1.114'), [])
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        baseline = self.gh.prs[0]
        self.assertTrue(baseline['draft'])
        self.assertEqual(self.gh.statuses[baseline['head']['sha']][0]['state'], 'pending')
        self.assertIn(member_pr['html_url'], baseline['body'])
        self.gh.member_pr(clipboard, merged=True, group='G3')
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertFalse(baseline['draft'])
        self.assertEqual(self.gh.statuses[baseline['head']['sha']][0]['state'], 'success')
        self.gh.merge('G3')
        self.assertEqual(self.gh.cfg['groups']['G3']['version'], '6.1.114')
        next_members = self.gh.start('G3', '6.1.115', dry_run=True)
        self.assertEqual({m['repo']: m['version'] for m in next_members},
                         {'linuxdeepin/dde-daemon': '6.1.115', clipboard: '1:6.1.115'})

    def test_first_epoch_target_must_exceed_full_member_version(self):
        for actual in ('1:6.1.114', '2:6.1.115'):
            with self.subTest(actual=actual):
                gh = GitHub()
                gh.live['linuxdeepin/dde-clipboard'] = actual
                with self.assertRaisesRegex(RuntimeError, '必须大于.*实际版本'):
                    gh.start('G3', '6.1.114')
                self.assertEqual(gh.writes, [])

    def test_new_group_target_cannot_contain_epoch(self):
        with self.assertRaisesRegex(RuntimeError, '组目标版本不能包含 epoch'):
            self.gh.start('G3', '1:6.1.114')
        self.assertEqual(self.gh.writes, [])

    def test_completed_legacy_epoch_release_can_start_bare_group_version(self):
        for previous_target in ('1:6.0.50', '2:6.0.50~rc1', '1:6.0.49'):
            with self.subTest(previous_target=previous_target):
                gh = GitHub()
                gh.start()
                release = gh.release()
                release['version'] = previous_target
                for member in release['members']:
                    member.pop('version')
                head = gh.prs[0]['head']['sha']
                gh.configs[head]['groups']['G2']['version'] = previous_target
                gh.commits[head]['message'] = gh.ns['MARKER'] + json.dumps(release) + '\n-->'
                for member in release['members']:
                    gh.member_pr(member['repo'], merged=True)
                gh.merge()
                self.assertEqual(gh.cfg['groups']['G2']['version'], previous_target)
                epoch = previous_target.split(':', 1)[0]
                before = len(gh.writes)
                planned = gh.start(version='6.0.51', dry_run=True)
                self.assertEqual({m['version'] for m in planned}, {f'{epoch}:6.0.51'})
                self.assertEqual(len(gh.writes), before)
                for target in ('6.0.48', previous_target.split(':', 1)[-1]):
                    with self.assertRaisesRegex(RuntimeError, '必须大于.*基线'):
                        gh.start(version=target)
                self.assertEqual(len(gh.writes), before)
                self.assertEqual(gh.start(version='6.0.51'), planned)
                self.assertEqual(gh.release()['version'], '6.0.51')
                for member in planned:
                    gh.member_pr(member['repo'], merged=True)
                gh.ns['sync'](gh.repo, 'master', gh.cfg)
                self.assertEqual(gh.statuses[gh.prs[0]['head']['sha']][0]['state'], 'success')
                gh.merge()
                self.assertEqual(gh.cfg['groups']['G2']['version'], '6.0.51')
                following = gh.start(version='6.0.52', dry_run=True)
                self.assertEqual({m['version'] for m in following}, {f'{epoch}:6.0.52'})

    def test_archived_or_disabled_member_is_rejected_before_reservation(self):
        original = self.gh.api
        for flag in ('archived', 'disabled'):
            def unavailable_member(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == 'repos/linuxdeepin/go-lib':
                    info[flag] = True
                return info
            with self.subTest(flag=flag), patch.dict(self.gh.ns, api=unavailable_member):
                for dry_run in (True, False):
                    with self.assertRaisesRegex(RuntimeError, '已归档或禁用'):
                        self.gh.start(dry_run=dry_run)
                self.assertEqual(self.gh.writes, [])

    def test_missing_or_disabled_workflow_is_rejected_before_reservation(self):
        key = ('linuxdeepin/go-lib', 'call-update-changelog.yml')
        for info in (None, {'state': 'disabled_fork'}, {'state': 'disabled_manually'}):
            with self.subTest(info=info):
                self.gh.workflow_info[key] = info
                for dry_run in (True, False):
                    with self.assertRaisesRegex(RuntimeError, '缺少成员工作流|未启用'):
                        self.gh.start(dry_run=dry_run)
                self.assertEqual(self.gh.writes, [])

    def test_workflow_dispatch_is_required_on_default_and_selected_branch(self):
        path = '.github/workflows/call-update-changelog.yml'
        for branch in ('master', 'release/stable'):
            for text in (None, 'on:\n  push:\n'):
                with self.subTest(branch=branch, text=text):
                    gh = GitHub()
                    gh.workflow_files[('linuxdeepin/go-lib', path, branch)] = text
                    with self.assertRaisesRegex(RuntimeError, '缺少成员工作流|不支持 workflow_dispatch'):
                        gh.start(member_base='release/stable')
                    self.assertEqual(gh.writes, [])

    def test_workflow_dispatch_shorthand_is_supported(self):
        for text in ('on: workflow_dispatch\n', 'on: [push, workflow_dispatch]\n'):
            with self.subTest(text=text):
                gh = GitHub()
                gh.workflow_files[('linuxdeepin/go-lib', '.github/workflows/call-update-changelog.yml', 'master')] = text
                self.assertEqual(len(gh.start(dry_run=True)), 2)
                self.assertEqual(gh.writes, [])

    def test_epoch_drift_is_blocked_after_release_completion(self):
        for actual in ('6.1.114', '2:6.1.114', '1:6.1.115'):
            with self.subTest(actual=actual):
                gh = GitHub()
                clipboard = 'linuxdeepin/dde-clipboard'
                gh.live[clipboard] = '1:6.1.37'
                gh.start('G3', '6.1.114')
                for repo in ('linuxdeepin/dde-daemon', clipboard):
                    gh.member_pr(repo, merged=True, group='G3')
                gh.merge('G3')
                gh.live[clipboard] = actual
                before = len(gh.writes)
                with self.assertRaises(RuntimeError):
                    gh.start('G3', '6.1.116')
                self.assertEqual(len(gh.writes), before)
                with self.assertRaisesRegex(RuntimeError, 'linuxdeepin/dde-clipboard'):
                    gh.ns['sync'](gh.repo, 'master', gh.cfg)
                self.assertEqual(gh.statuses[gh.prs[0]['head']['sha']][0]['state'], 'failure')

    def test_epoch_change_during_next_prepare_is_blocked(self):
        clipboard = 'linuxdeepin/dde-clipboard'
        self.gh.live[clipboard] = '1:6.1.37'
        self.gh.start('G3', '6.1.114')
        for repo in ('linuxdeepin/dde-daemon', clipboard):
            self.gh.member_pr(repo, merged=True, group='G3')
        self.gh.merge('G3')
        original = self.gh.ns['states']
        def changed_epoch(*args, **kwargs):
            result = original(*args, **kwargs)
            self.gh.live[clipboard] = '2:6.1.114'
            return result
        before = len(self.gh.writes)
        with patch.dict(self.gh.ns, states=changed_epoch):
            with self.assertRaisesRegex(RuntimeError, '与上轮目标 1:6.1.114 不一致'):
                self.gh.start('G3', '6.1.115')
        self.assertEqual(len(self.gh.writes), before)

    def test_invalid_member_target_record_cannot_resume(self):
        self.gh.start()
        release = self.gh.release()
        head = self.gh.prs[0]['head']['sha']
        before = len(self.gh.writes)
        for version in ('1:6.0.51', '1:invalid', '1:6.0.50.'):
            with self.subTest(version=version):
                release['members'][0]['version'] = version
                self.gh.commits[head]['message'] = self.gh.ns['MARKER'] + json.dumps(release) + '\n-->'
                with self.assertRaises(RuntimeError):
                    self.gh.start()
                self.assertEqual(len(self.gh.writes), before)

    def test_legacy_record_without_member_versions_keeps_original_target(self):
        for target in ('6.0.50', '1:6.0.50~rc1'):
            with self.subTest(target=target):
                gh = GitHub()
                gh.start()
                release = gh.release()
                release['version'] = target
                for member in release['members']:
                    member.pop('version', None)
                head = gh.prs[0]['head']['sha']
                gh.configs[head]['groups']['G2']['version'] = target
                gh.commits[head]['message'] = gh.ns['MARKER'] + json.dumps(release) + '\n-->'
                members = gh.start(version=target)
                self.assertEqual({m.get('version', target) for m in members}, {target})
                for member in members:
                    gh.member_pr(member['repo'], merged=True)
                gh.ns['sync'](gh.repo, 'master', gh.cfg)
                self.assertEqual(gh.statuses[head][0]['state'], 'success')

    def test_sync_without_release_recognizes_matching_versions_with_epoch(self):
        self.gh.live['linuxdeepin/dde-clipboard'] = '1:6.1.113'
        for baseline in ('6.1.113', '1:6.1.113'):
            with self.subTest(baseline=baseline):
                self.gh.cfg['groups']['G3']['version'] = baseline
                with patch('builtins.print') as output:
                    self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
                output.assert_not_called()
                self.assertEqual(self.gh.writes, [])

    def test_first_release_blocks_existing_member_release_pr(self):
        for branch in ('release-6.0.16', 'automation/update-changelog/6.0.16'):
            with self.subTest(branch=branch):
                gh = GitHub()
                gh.live['linuxdeepin/go-lib'] = '6.0.15'
                gh.member_prs['linuxdeepin/go-lib'] = [
                    {'state': 'open', 'head': {'ref': branch}}]
                with self.assertRaisesRegex(RuntimeError, '存在未完成的发布 PR'):
                    gh.start()
                self.assertEqual(gh.writes, [])

    def test_completed_release_drift_cannot_be_treated_as_first_release(self):
        for actual in ('6.0.15', '6.0.51'):
            with self.subTest(actual=actual):
                self.gh = GitHub()
                self.complete_g2()
                self.gh.live['linuxdeepin/go-lib'] = actual
                before = len(self.gh.writes)
                with self.assertRaises(RuntimeError):
                    self.gh.start(version='6.0.52')
                self.assertEqual(before, len(self.gh.writes))
                with self.assertRaisesRegex(RuntimeError, 'linuxdeepin/go-lib'):
                    self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
                self.assertEqual(self.gh.cfg['groups']['G2']['version'], '6.0.50')

    def test_sync_before_first_release_only_reports_uninitialized_group(self):
        self.gh.live['linuxdeepin/go-lib'] = '6.0.15'
        with patch('builtins.print') as output:
            self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        output.assert_called_once_with('G2: 尚未统一，请从 Release group 发起首轮发布')
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(self.gh.cfg['groups']['G2']['version'], '6.0.49')

    def test_uninitialized_group_does_not_block_other_group_readiness(self):
        self.gh.live['linuxdeepin/go-lib'] = '6.0.15'
        self.gh.start('G1', '6.7.53')
        self.gh.member_pr('linuxdeepin/dtkcore', merged=True, group='G1')
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertFalse(self.gh.prs[0]['draft'])
        self.assertEqual(self.gh.cfg['groups']['G2']['version'], '6.0.49')

    def test_same_group_new_target_is_blocked_even_without_member_prs(self):
        self.gh.start()
        before = len(self.gh.writes)
        with self.assertRaisesRegex(RuntimeError, '正在发布'):
            self.gh.start(version='6.0.51')
        self.assertEqual(before, len(self.gh.writes))

    def test_different_groups_can_start(self):
        self.gh.start()
        self.gh.start('G1', '6.7.53')
        self.assertEqual({p['head']['ref'] for p in self.gh.prs}, {'baseline/G1', 'baseline/G2'})

    def test_other_group_old_base_does_not_block_next_release(self):
        self.gh.start()
        self.gh.start('G1', '6.7.53')
        for repo in ('linuxdeepin/go-lib', 'linuxdeepin/dde-api'):
            self.gh.member_pr(repo, merged=True)
        self.gh.merge()
        self.assertEqual(len(self.gh.start(version='6.0.51')), 2)

    def test_resume_skips_merged_pending_and_running(self):
        self.gh.start()
        self.gh.member_pr('linuxdeepin/go-lib', merged=True)
        self.gh.member_pr('linuxdeepin/dde-api')
        self.assertEqual(self.gh.start(), [])
        self.gh.member_prs.pop('linuxdeepin/dde-api')
        self.gh.runs['linuxdeepin/dde-api'] = [{'created_at': '9999-01-01T00:00:00Z', 'head_branch': 'master', 'status': 'in_progress'}]
        self.assertEqual(self.gh.start(), [])
        self.gh.runs.clear()
        self.assertEqual([m['repo'] for m in self.gh.start()], ['linuxdeepin/dde-api'])

    def test_all_members_merged_still_blocks_next_until_baseline_merge(self):
        self.gh.start()
        for repo in ('linuxdeepin/go-lib', 'linuxdeepin/dde-api'):
            self.gh.member_pr(repo, merged=True)
        self.gh.start()
        self.assertFalse(self.gh.prs[0]['draft'])
        with self.assertRaises(RuntimeError):
            self.gh.start(version='6.0.51')
        self.gh.merge()
        self.assertEqual(len(self.gh.start(version='6.0.51')), 2)

    def test_closed_unmerged_marker_is_not_unlock_and_can_be_reopened(self):
        self.gh.start()
        self.gh.prs[0]['state'] = 'closed'
        self.gh.refs.pop('baseline/G2')
        with self.assertRaises(RuntimeError):
            self.gh.start(version='6.0.51')
        self.assertEqual(len(self.gh.start()), 2)
        self.assertEqual(self.gh.prs[0]['state'], 'open')

    def test_premature_baseline_merge_does_not_unlock(self):
        self.gh.start()
        self.gh.merge()
        with self.assertRaises(RuntimeError):
            self.gh.start(version='6.0.51')
        with self.assertRaisesRegex(RuntimeError, '提前合并'):
            self.gh.start()

    def test_orphan_branch_is_recovered_for_same_target_only(self):
        self.gh.fail_pr = True
        with self.assertRaises(RuntimeError):
            self.gh.start()
        self.assertIn('baseline/G2', self.gh.refs)
        self.assertEqual(self.gh.prs, [])
        self.gh.fail_pr = False
        with self.assertRaises(RuntimeError):
            self.gh.start(version='6.0.51')
        self.assertEqual(len(self.gh.start()), 2)

    def test_atomic_reservation_conflict_never_creates_pr(self):
        self.gh.fail_ref = True
        with self.assertRaisesRegex(RuntimeError, 'reference already exists'):
            self.gh.start()
        self.assertEqual(self.gh.prs, [])

    def test_direct_push_is_not_proof_of_pr_merge(self):
        self.gh.start()
        self.gh.live['linuxdeepin/go-lib'] = '6.0.50'
        with self.assertRaisesRegex(RuntimeError, '未找到'):
            self.gh.start()

    def test_member_closed_pr_and_drift_fail_closed(self):
        self.gh.start()
        self.gh.member_pr('linuxdeepin/go-lib', pending=False)
        with self.assertRaisesRegex(RuntimeError, '已关闭'):
            self.gh.start()
        self.gh.member_prs.clear()
        self.gh.live['linuxdeepin/go-lib'] = '6.0.51'
        with self.assertRaisesRegex(RuntimeError, '超过本轮'):
            self.gh.start()

    def test_group_snapshot_cannot_be_changed(self):
        self.gh.start()
        self.gh.cfg['groups']['G2']['members'].append('go-dbus-factory')
        with self.assertRaisesRegex(RuntimeError, '成员发生变化'):
            self.gh.start()

    def test_head_commit_cannot_be_modified(self):
        self.gh.start()
        sha = self.gh.prs[0]['head']['sha']
        self.gh.configs[sha]['groups']['G1']['version'] = '99.0.0'
        with self.assertRaisesRegex(RuntimeError, '单组基线'):
            self.gh.start()

    def test_version_and_safe_branch_name(self):
        self.gh.start(version='6.0.50~rc1')
        self.assertEqual(self.gh.prs[0]['head']['ref'], 'baseline/G2')
        with self.assertRaises(RuntimeError):
            GitHub().start('G1', '6.8')
        with self.assertRaises(RuntimeError):
            GitHub().start(version='6.0.50$(true)')
        with self.assertRaises(RuntimeError):
            GitHub().start(version='6.0.49')

    def test_g1_invalid_semver_is_rejected_before_reservation(self):
        for version in ('6.7.053', '06.7.53', '6.07.53', '6.7.53-01',
                        '6.7.53-alpha..1', '6.7.53+build..1'):
            with self.subTest(version=version):
                gh = GitHub()
                with self.assertRaisesRegex(RuntimeError, '语义化版本'):
                    gh.start('G1', version)
                self.assertEqual(gh.writes, [])
                self.assertNotIn('baseline/G1', gh.refs)

    def test_g1_new_release_rejects_prerelease(self):
        for version in ('6.7.53-rc.1', '6.7.53-rc.1+build.01'):
            with self.subTest(version=version):
                gh = GitHub()
                with self.assertRaisesRegex(RuntimeError, '暂不支持预发布'):
                    gh.start('G1', version)
                self.assertEqual(gh.writes, [])

        self.assertEqual(len(self.gh.start('G1', '6.7.53+build-01', dry_run=True)), 1)
        self.assertEqual(self.gh.writes, [])

    def test_existing_g1_prerelease_can_resume(self):
        self.gh.start('G1', '6.7.53')
        release = self.gh.release('G1')
        release['version'] = '6.7.53-rc.1'
        for member in release['members']:
            member['version'] = '6.7.53-rc.1'
        head = self.gh.prs[0]['head']['sha']
        self.gh.configs[head]['groups']['G1']['version'] = '6.7.53-rc.1'
        self.gh.commits[head]['message'] = self.gh.ns['MARKER'] + json.dumps(release) + '\n-->'
        self.assertEqual(len(self.gh.start('G1', '6.7.53-rc.1')), 1)

    def test_invalid_member_branch_is_rejected_before_reservation(self):
        for group, version in (('G1', '6.7.53+build.lock'), ('G2', '6.0.50.'),
                               ('G5', '2026.03.01.')):
            for dry_run in (False, True):
                with self.subTest(group=group, version=version, dry_run=dry_run):
                    gh = GitHub()
                    with self.assertRaisesRegex(RuntimeError, '成员分支'):
                        gh.start(group, version, dry_run=dry_run)
                    self.assertEqual(gh.writes, [])
                    self.assertNotIn(f'baseline/{group}', gh.refs)

    def test_rewrite_preserves_other_group_with_version_spacing(self):
        text = 'groups:\n  G4:\n    version : "6.5.12"\n  G5:\n    version: "2026.02.27"\n'
        value = yaml.safe_load(self.gh.ns['rewrite'](text, 'G4', '6.5.13'))
        self.assertEqual(value['groups']['G4']['version'], '6.5.13')
        self.assertEqual(value['groups']['G5']['version'], '2026.02.27')

    def test_pr_branch_contracts(self):
        self.assertEqual(self.gh.ns['member_branch']('G1', '6.7.53'), 'release-6.7.53')
        self.assertEqual(self.gh.ns['member_branch']('G2', '1:6.0.50~rc1'), 'automation/update-changelog/1-6.0.50-rc1')
        self.assertEqual(self.gh.ns['member_branch']('G2', '6.0.50~~rc1'), 'automation/update-changelog/6.0.50-rc1')

    def test_configured_qt_names_recognize_pending_and_merged_prs(self):
        canonical = {'qt5platform-plugins': 'linuxdeepin/dde-qtplatform-plugins',
                     'qt5integration': 'linuxdeepin/dde-qtintegration'}
        cfg = yaml.safe_load((ROOT / 'groups.yaml').read_text())
        self.gh.cfg['repo_overrides'] = cfg.get('repo_overrides', {})
        self.gh.cfg['groups']['G1']['members'] = list(canonical)
        self.gh.configs['base'] = copy.deepcopy(self.gh.cfg)
        for member, repo in canonical.items():
            self.gh.live[f'linuxdeepin/{member}'] = '6.7.52'
            self.gh.live[repo] = '6.7.52'
        members = self.gh.start('G1', '6.7.53')
        for merged in (False, True):
            for member, repo in zip(members, canonical.values()):
                pr = self.gh.member_pr(member['repo'], merged=merged, group='G1')
                pr['head']['repo']['full_name'] = repo
            self.assertEqual(self.gh.start('G1', '6.7.53'), [])
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertFalse(self.gh.prs[0]['draft'])

    def test_api_auth_error_is_not_a_missing_ref(self):
        ns = {'__name__': 'api_test'}
        exec(compile(CODE, 'sync-baseline.py', 'exec'), ns)
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'Forbidden (HTTP 403)')):
            with self.assertRaisesRegex(RuntimeError, 'GitHub API'):
                ns['api']('repos/linuxdeepin/dde/git/ref/heads/baseline/G2', missing=True)
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'Not found (HTTP 404)')):
            self.assertIsNone(ns['api']('repos/linuxdeepin/dde/git/ref/heads/baseline/G2', missing=True))
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '[[{"number": 1}], [{"number": 2}]]', '')):
            self.assertEqual(ns['api']('repos/linuxdeepin/dde/pulls', paginate=True), [{'number': 1}, {'number': 2}])

    def test_github_error_keeps_reserved_release_locked(self):
        self.gh.start()
        def unavailable(*args, **kwargs):
            raise RuntimeError('GitHub API unavailable')
        original = self.gh.ns['head_version']
        self.gh.ns['head_version'] = unavailable
        with self.assertRaises(RuntimeError):
            self.gh.start(version='6.0.51')
        self.assertIn('baseline/G2', self.gh.refs)
        self.gh.ns['head_version'] = original
        self.assertEqual(len(self.gh.start()), 2)

    def test_sync_publishes_status_and_readies_existing_pr_only(self):
        self.gh.start()
        self.gh.start('G1', '6.7.53')
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertEqual(len(self.gh.prs), 2)
        for repo in ('linuxdeepin/go-lib', 'linuxdeepin/dde-api'):
            self.gh.member_pr(repo, merged=True)
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        g2 = next(p for p in self.gh.prs if p['head']['ref'] == 'baseline/G2')
        g1 = next(p for p in self.gh.prs if p['head']['ref'] == 'baseline/G1')
        self.assertFalse(g2['draft'])
        self.assertTrue(g1['draft'])
        self.assertEqual(self.gh.cfg['groups']['G2']['version'], '6.0.49')
        self.assertEqual(len(self.gh.prs), 2)

    def test_member_links_preserve_latest_manual_notes_and_do_not_repeat_edits(self):
        self.gh.start()
        pr = self.gh.prs[0]
        stale = copy.deepcopy(pr)
        self.gh.member_pr('linuxdeepin/go-lib', merged=True)
        self.gh.member_pr('linuxdeepin/dde-api')
        prefix, suffix = '人工说明：保留此段\n\n', '\n\n- [ ] 人工验证\n'
        pr['body'] = prefix + pr['body'] + suffix
        results = self.gh.ns['check'](self.gh.repo, stale, self.gh.release(), self.gh.cfg)
        self.assertEqual(results, ['merged', 'pending'])
        self.assertTrue(pr['body'].startswith(prefix))
        self.assertTrue(pr['body'].endswith(suffix))
        for repo in ('linuxdeepin/go-lib', 'linuxdeepin/dde-api'):
            self.assertIn(f'[#1](https://github.com/{repo}/pull/1)', pr['body'])
        self.assertIn('[#1](https://github.com/linuxdeepin/go-lib/pull/1)（已合并）', pr['body'])
        self.assertIn('[#1](https://github.com/linuxdeepin/dde-api/pull/1)（待合并）', pr['body'])
        self.assertTrue(pr['draft'])
        self.assertEqual(self.gh.cfg['groups']['G2']['version'], '6.0.49')
        self.event(pr)
        edits = [data for _, data, method in self.gh.writes if method == 'PATCH' and 'body' in data]
        self.assertEqual(edits, [{'body': pr['body']}])
        self.gh.member_pr('linuxdeepin/dde-api', merged=True)
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertIn('[#1](https://github.com/linuxdeepin/dde-api/pull/1)（已合并）', pr['body'])
        self.assertNotIn('待合并', pr['body'])
        self.assertTrue(pr['body'].startswith(prefix))
        self.assertTrue(pr['body'].endswith(suffix))
        self.assertFalse(pr['draft'])
        self.assertEqual(self.gh.statuses[pr['head']['sha']][0]['state'], 'success')
        edits = [data for _, data, method in self.gh.writes if method == 'PATCH' and 'body' in data]
        self.assertEqual(len(edits), 2)

    def test_legacy_baseline_description_gets_links_without_losing_notes(self):
        self.gh.start()
        pr = self.gh.prs[0]
        original = 'G2 发布 6.0.50。\n\n- linuxdeepin/go-lib\n\n人工备注\n'
        pr['body'] = original
        self.gh.member_pr('linuxdeepin/go-lib')
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertTrue(pr['body'].startswith(original + '\n\n'))
        self.assertIn('https://github.com/linuxdeepin/go-lib/pull/1', pr['body'])
        self.assertIn('- linuxdeepin/dde-api: 尚无发布 PR', pr['body'])
        self.assertEqual(pr['body'].count(self.gh.ns['MEMBER_LINKS_START']), 1)
        self.assertEqual(pr['body'].count(self.gh.ns['MEMBER_LINKS_END']), 1)

    def test_links_only_include_this_round_and_reuse_member_pr_queries(self):
        self.gh.start()
        repo = 'linuxdeepin/go-lib'
        valid = self.gh.member_pr(repo)
        invalid = [copy.deepcopy(valid) for _ in range(5)]
        invalid[0]['created_at'] = '2000-01-01T00:00:00Z'
        invalid[1]['head']['repo']['full_name'] = 'someone/go-lib'
        invalid[2]['head']['repo'] = None
        invalid[3]['head']['ref'] = 'automation/update-changelog/6.0.51'
        invalid[4]['base']['ref'] = 'another-base'
        for number, pr in enumerate(invalid, 2):
            pr.update(number=number, html_url=f'https://github.com/{repo}/pull/{number}')
        self.gh.member_prs[repo] = [*invalid, valid]
        calls, original_api = [], self.gh.api
        def recorded_api(url, *args, **kwargs):
            calls.append(url)
            if url.startswith(f'repos/{repo}/pulls?') and 'head' in parse_qs(urlsplit(url).query):
                return []
            return original_api(url, *args, **kwargs)
        self.gh.ns['api'] = recorded_api
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        description = self.gh.prs[0]['body']
        self.assertIn(valid['html_url'], description)
        for pr in invalid:
            self.assertNotIn(pr['html_url'], description)
        for member in (repo, 'linuxdeepin/dde-api'):
            self.assertEqual(sum(url.startswith(f'repos/{member}/pulls?') for url in calls), 1)

    def test_closed_member_link_is_shown_while_readiness_still_fails(self):
        self.gh.start()
        member = self.gh.member_pr('linuxdeepin/go-lib', pending=False)
        with self.assertRaisesRegex(RuntimeError, '已关闭'):
            self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        pr = self.gh.prs[0]
        self.assertIn(member['html_url'], pr['body'])
        self.assertIn(f"[#1]({member['html_url']})（已关闭，未合并）", pr['body'])
        self.assertTrue(pr['draft'])
        self.assertEqual(self.gh.statuses[pr['head']['sha']][0]['state'], 'failure')

    def test_member_link_query_failure_keeps_description_intact(self):
        self.gh.start()
        pr = self.gh.prs[0]
        original_body, original_api = pr['body'], self.gh.api
        def unavailable(url, *args, **kwargs):
            if url.startswith('repos/linuxdeepin/go-lib/pulls?'):
                raise RuntimeError('member PR API unavailable')
            return original_api(url, *args, **kwargs)
        self.gh.ns['api'] = unavailable
        with self.assertRaisesRegex(RuntimeError, 'member PR API unavailable'):
            self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertEqual(pr['body'], original_body)
        self.assertFalse(any(method == 'PATCH' for _, _, method in self.gh.writes))

    def test_invalid_member_link_markers_do_not_overwrite_description(self):
        self.gh.start()
        pr = self.gh.prs[0]
        start, end = self.gh.ns['MEMBER_LINKS_START'], self.gh.ns['MEMBER_LINKS_END']
        before = len(self.gh.writes)
        for description in (start, end, end + '\n人工说明\n' + start, start + end + start + end):
            with self.subTest(description=description):
                pr['body'] = description
                with self.assertRaisesRegex(RuntimeError, '成员链接区块'):
                    self.gh.ns['check'](self.gh.repo, pr, self.gh.release(), self.gh.cfg)
                self.assertEqual(pr['body'], description)
                self.assertEqual(len(self.gh.writes), before)

    def test_resume_dry_run_does_not_publish_member_links(self):
        self.gh.start()
        self.gh.member_pr('linuxdeepin/go-lib')
        original_body, before = self.gh.prs[0]['body'], len(self.gh.writes)
        self.assertEqual([m['repo'] for m in self.gh.start(dry_run=True)], ['linuxdeepin/dde-api'])
        self.assertEqual(self.gh.prs[0]['body'], original_body)
        self.assertEqual(len(self.gh.writes), before)

    def test_closed_marker_cannot_get_success_status(self):
        self.gh.start()
        for repo in ('linuxdeepin/go-lib', 'linuxdeepin/dde-api'):
            self.gh.member_pr(repo, merged=True)
        self.gh.prs[0]['state'] = 'closed'
        with self.assertRaises(RuntimeError):
            self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        statuses = [data for url, data, _ in self.gh.writes if '/statuses/' in url]
        self.assertEqual(statuses[-1]['state'], 'failure')

    def event(self, pr):
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as f:
            json.dump({'number': pr['number']}, f)
            f.flush()
            with patch.dict(os.environ, GITHUB_REPOSITORY=self.gh.repo, GITHUB_EVENT_NAME='pull_request_target',
                            GITHUB_EVENT_PATH=f.name, GROUP=''):
                self.gh.ns['main']()

    def ordinary_pr(self):
        pr = {'number': 100, 'head': {'ref': 'feature/docs', 'sha': 'ordinary', 'repo': {'full_name': self.gh.repo}},
              'base': {'sha': 'base', 'ref': 'master'}, 'state': 'open', 'draft': False, 'merged_at': None}
        self.gh.configs['ordinary'] = copy.deepcopy(self.gh.cfg)
        self.gh.prs.insert(0, pr)
        return pr

    def test_ordinary_pr_check_passes_but_manual_baseline_bump_fails(self):
        pr = self.ordinary_pr()
        self.event(pr)
        self.assertEqual(self.gh.writes[-1][1]['state'], 'success')
        self.gh.configs['ordinary']['groups']['G2']['version'] = '6.0.50'
        self.event(pr)
        self.assertEqual(self.gh.writes[-1][1]['state'], 'failure')

    def test_member_change_pr_is_blocked_during_release(self):
        self.gh.start()
        pr = self.ordinary_pr()
        self.gh.configs['ordinary']['groups']['G2']['members'].append('go-dbus-factory')
        with self.assertRaisesRegex(RuntimeError, '不能修改'):
            self.event(pr)
        self.assertEqual(self.gh.writes[-1][1]['state'], 'failure')

    def test_pr_event_checks_only_requested_group(self):
        self.gh.start()
        pr = self.gh.prs[0]
        original = self.gh.ns['head_version']
        def fail_g1(repo, ref):
            if repo.endswith('/dtkcore'):
                raise RuntimeError('G1 unavailable')
            return original(repo, ref)
        self.gh.ns['head_version'] = fail_g1
        self.event(pr)
        self.assertEqual(self.gh.statuses[pr['head']['sha']][0]['state'], 'pending')

    def test_periodic_sync_error_does_not_skip_other_group(self):
        self.gh.start()
        for repo in ('linuxdeepin/go-lib', 'linuxdeepin/dde-api'):
            self.gh.member_pr(repo, merged=True)
        original = self.gh.ns['head_version']
        def fail_g1(repo, ref):
            if repo.endswith('/dtkcore'):
                raise RuntimeError('G1 unavailable')
            return original(repo, ref)
        self.gh.ns['head_version'] = fail_g1
        with self.assertRaisesRegex(RuntimeError, 'G1 unavailable'):
            self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertFalse(self.gh.prs[0]['draft'])

    def test_graphql_error_is_not_success(self):
        ns = {'__name__': 'api_test'}
        exec(compile(CODE, 'sync-baseline.py', 'exec'), ns)
        response = subprocess.CompletedProcess([], 0, '{"errors": [{"message": "denied"}]}', '')
        with patch('subprocess.run', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'GraphQL'):
                ns['api']('graphql', {'query': 'mutation{}'})

    def complete_g2(self):
        self.gh.start()
        for repo in ('linuxdeepin/go-lib', 'linuxdeepin/dde-api'):
            self.gh.member_pr(repo, merged=True)
        self.gh.start()
        self.gh.merge()

    def update_branch(self, group):
        pr = next(p for p in self.gh.prs if p['head']['ref'] == f'baseline/{group}')
        release = self.gh.release(group)
        head = f"updated-{pr['number']}"
        self.gh.configs[head] = copy.deepcopy(self.gh.cfg)
        self.gh.configs[head]['groups'][group]['version'] = release['version']
        self.gh.commits[head] = {'sha': head, 'message': 'Merge master into baseline',
                                'parents': [{'sha': pr['head']['sha']}, {'sha': self.gh.refs['master']}]}
        self.gh.refs[f'baseline/{group}'] = head
        pr['head']['sha'] = head
        return head

    def test_second_round_orphan_is_recovered_without_deleting_new_ref(self):
        self.complete_g2()
        self.gh.fail_pr = True
        with self.assertRaisesRegex(RuntimeError, 'PR API unavailable'):
            self.gh.start(version='6.0.51')
        head = self.gh.refs['baseline/G2']
        self.gh.fail_pr = False
        before = len(self.gh.writes)
        with self.assertRaisesRegex(RuntimeError, '正在发布 6.0.51'):
            self.gh.start(version='6.0.52')
        self.assertEqual(before, len(self.gh.writes))
        self.assertEqual(len(self.gh.start(version='6.0.51', dry_run=True)), 2)
        self.assertEqual(before, len(self.gh.writes))
        self.assertEqual(len(self.gh.start(version='6.0.51')), 2)
        self.assertEqual(self.gh.refs['baseline/G2'], head)
        self.assertEqual(self.gh.prs[0]['head']['sha'], head)
        self.assertEqual(len(self.gh.prs), 2)
        self.assertFalse(any(method == 'DELETE' for _, _, method in self.gh.writes[before:]))

    def test_sync_reports_new_orphan_not_old_completed_round(self):
        self.complete_g2()
        self.gh.fail_pr = True
        with self.assertRaises(RuntimeError):
            self.gh.start(version='6.0.51')
        self.gh.live['linuxdeepin/go-lib'] = '6.0.51'
        before = len(self.gh.writes)
        with self.assertRaisesRegex(RuntimeError, '分支已保留但 PR 未创建'):
            self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertEqual(before, len(self.gh.writes))

    def test_resume_uses_refreshed_member_states(self):
        self.gh.start()
        with patch.dict(self.gh.ns, states=unittest.mock.Mock(side_effect=[['missing', 'missing'], ['running', 'pending']])):
            self.assertEqual(self.gh.start(), [])

    def test_retargeted_pr_cannot_be_resumed_or_readied(self):
        self.gh.start()
        self.gh.prs[0]['base']['ref'] = 'development'
        self.gh.refs.pop('baseline/G2')
        before = len(self.gh.writes)
        for dry_run in (True, False):
            with self.assertRaisesRegex(RuntimeError, 'PR 目标已改变'):
                self.gh.start(dry_run=dry_run)
        self.assertEqual(before, len(self.gh.writes))

    def test_invalid_other_group_record_does_not_block_this_group(self):
        self.gh.start('G1', '6.7.53')
        self.complete_g2()
        head = self.gh.prs[1]['head']['sha']
        self.gh.commits[head]['message'] = 'not a release record'
        self.assertEqual(len(self.gh.start(version='6.0.51', dry_run=True)), 2)
        self.gh.configs[head]['groups']['G2']['version'] = '6.0.99'
        with self.assertRaisesRegex(RuntimeError, '旧基线 PR'):
            self.gh.start(version='6.0.51')

    def test_identical_status_is_not_reposted_and_completed_pr_is_skipped(self):
        self.gh.start()
        head = self.gh.prs[0]['head']['sha']
        for _ in range(3):
            self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertEqual(len(self.gh.statuses[head]), 1)
        with patch.dict(os.environ, GITHUB_RUN_ID='456'):
            self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertEqual(len(self.gh.statuses[head]), 1)
        for repo in ('linuxdeepin/go-lib', 'linuxdeepin/dde-api'):
            self.gh.member_pr(repo, merged=True)
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertEqual([s['state'] for s in self.gh.statuses[head]], ['success', 'pending'])
        self.gh.merge()
        before = len(self.gh.writes)
        for _ in range(3):
            self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertEqual(before, len(self.gh.writes))

    def test_safe_merge_update_preserves_release_record(self):
        self.gh.start('G1', '6.7.53')
        original = self.gh.release('G1')
        self.complete_g2()
        head = self.update_branch('G1')
        self.assertEqual(self.gh.release('G1'), original)
        self.assertEqual(len(self.gh.start('G1', '6.7.53')), 1)
        self.event(self.gh.prs[1])
        self.assertEqual(self.gh.statuses[head][0]['state'], 'pending')
        self.gh.member_pr('linuxdeepin/dtkcore', merged=True, group='G1')
        self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertEqual(self.gh.statuses[head][0]['state'], 'success')

    def test_merge_update_cannot_modify_another_group_or_use_untrusted_base(self):
        self.gh.start('G1', '6.7.53')
        self.complete_g2()
        head = self.update_branch('G1')
        self.gh.configs[head]['groups']['G2']['version'] = '6.0.99'
        with self.assertRaisesRegex(RuntimeError, '单组基线变更'):
            self.gh.start('G1', '6.7.53')
        self.gh.configs[head]['groups']['G2']['version'] = '6.0.50'
        self.gh.commits['untrusted'] = {'sha': 'untrusted', 'message': 'feature', 'parents': [{'sha': 'base'}]}
        self.gh.configs['untrusted'] = copy.deepcopy(self.gh.cfg)
        self.gh.commits[head]['parents'][1]['sha'] = 'untrusted'
        with self.assertRaisesRegex(RuntimeError, '只能合并默认分支'):
            self.gh.start('G1', '6.7.53')

    def test_rebase_cannot_replace_original_release_record(self):
        self.gh.start('G1', '6.7.53')
        self.complete_g2()
        pr = self.gh.prs[1]
        self.gh.commits[pr['head']['sha']]['parents'] = [{'sha': self.gh.refs['master']}]
        with self.assertRaisesRegex(RuntimeError, '发布记录已改变'):
            self.gh.start('G1', '6.7.53')

    def test_extra_files_in_release_commit_are_rejected_and_status_fails(self):
        self.gh.start()
        original = self.gh.api
        def extra_file(url, *args, **kwargs):
            result = original(url, *args, **kwargs)
            if '/compare/' in url:
                result['files'].append({'filename': 'README.md', 'status': 'modified'})
            return result
        self.gh.ns['api'] = extra_file
        before = len(self.gh.writes)
        with self.assertRaisesRegex(RuntimeError, '只能修改 groups.yaml'):
            self.gh.start()
        self.assertEqual(before, len(self.gh.writes))
        with self.assertRaises(RuntimeError):
            self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertEqual(self.gh.statuses[self.gh.prs[0]['head']['sha']][0]['state'], 'failure')

    def test_status_api_failure_does_not_skip_other_group(self):
        self.gh.start('G1', '6.7.53')
        g1_sha = self.gh.prs[0]['head']['sha']
        self.gh.start()
        for repo in ('linuxdeepin/go-lib', 'linuxdeepin/dde-api'):
            self.gh.member_pr(repo, merged=True)
        original_version, original_api = self.gh.head_version, self.gh.api
        def failed_version(repo, ref):
            if repo.endswith('/dtkcore'):
                raise RuntimeError('G1 unavailable')
            return original_version(repo, ref)
        def failed_status(url, *args, **kwargs):
            if url.startswith(f'repos/{self.gh.repo}/statuses/{g1_sha}'):
                raise RuntimeError('status API unavailable')
            return original_api(url, *args, **kwargs)
        self.gh.ns.update(api=failed_status, head_version=failed_version)
        with self.assertRaisesRegex(RuntimeError, '状态发布失败'):
            self.gh.ns['sync'](self.gh.repo, 'master', self.gh.cfg)
        self.assertFalse(self.gh.prs[0]['draft'])

    def test_member_changes_are_blocked_for_orphan_release(self):
        self.gh.fail_pr = True
        with self.assertRaises(RuntimeError):
            self.gh.start()
        pr = self.ordinary_pr()
        self.gh.configs['ordinary']['groups']['G2']['members'].append('go-dbus-factory')
        with self.assertRaisesRegex(RuntimeError, '不能修改'):
            self.event(pr)

    def test_reusable_workflow_and_safe_shell(self):
        release = yaml.safe_load((ROOT / '.github/workflows/release-group.yml').read_text())
        self.assertEqual(release['jobs']['plan']['uses'], './.github/workflows/sync-baseline.yml')
        self.assertIn('inputs.group', release['concurrency']['group'])
        inputs = release.get('on', release.get(True))['workflow_dispatch']['inputs']
        self.assertEqual(set(inputs), {'group', 'version', 'name', 'email', 'base_branch', 'distribution', 'create_pr', 'dry_run'})
        self.assertTrue(inputs['name']['required'])
        self.assertTrue(inputs['email']['required'])
        self.assertTrue(inputs['create_pr']['default'])
        for name in ('base_branch', 'distribution', 'create_pr'):
            self.assertEqual(release['jobs']['plan']['with'][name], '${{ inputs.' + name + ' }}')
            self.assertIn(name, WORKFLOW.get('on', WORKFLOW.get(True))['workflow_call']['inputs'])
        self.assertNotIn('${{', release['jobs']['dispatch']['steps'][-1]['run'])
        self.assertNotIn('actions/checkout', str(WORKFLOW))

    def test_app_token_is_created_before_api_calls_in_each_job(self):
        release = yaml.safe_load((ROOT / '.github/workflows/release-group.yml').read_text())
        declared = WORKFLOW.get('on', WORKFLOW.get(True))['workflow_call']['secrets']
        self.assertEqual(set(declared), {'APP_ID', 'APP_PRIVATE_KEY'})
        self.assertEqual(release['jobs']['plan']['secrets'], {
            name: '${{ secrets.' + name + ' }}' for name in declared})
        for steps in (WORKFLOW['jobs']['coordinate']['steps'], release['jobs']['dispatch']['steps']):
            token = next(step for step in steps if step.get('id') == 'app_token')
            api_step = next(step for step in steps if 'GH_TOKEN' in step.get('env', {}))
            self.assertLess(steps.index(token), steps.index(api_step))
            self.assertNotIn('if', token)
            self.assertEqual(api_step['env']['GH_TOKEN'], '${{ steps.app_token.outputs.token }}')


class ReleaseDispatchTests(unittest.TestCase):
    def dispatch(self, group, create_pr='true'):
        release = yaml.safe_load((ROOT / '.github/workflows/release-group.yml').read_text())
        step = release['jobs']['dispatch']['steps'][-1]
        self.assertEqual(step['env']['MAINTAINER_NAME'], '${{ inputs.name }}')
        self.assertEqual(step['env']['MAINTAINER_EMAIL'], '${{ inputs.email }}')
        self.assertEqual(step['env']['DISTRIBUTION'], '${{ inputs.distribution }}')
        self.assertEqual(step['env']['CREATE_PR'], '${{ inputs.create_pr }}')
        with tempfile.TemporaryDirectory() as directory:
            gh = Path(directory) / 'gh'
            gh.write_text('#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
            gh.chmod(0o755)
            env = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'],
                   'GROUP': group, 'VERSION': '6.0.50', 'REPO': 'linuxdeepin/go-lib',
                   'BASE': 'release/stable', 'WORKFLOW': 'member-release.yml',
                   'MAINTAINER_NAME': 'Release Maintainer $(printf injected)',
                   'MAINTAINER_EMAIL': 'release+test@example.org', 'DISTRIBUTION': 'stable',
                   'CREATE_PR': create_pr}
            result = subprocess.run(['bash', '-c', step['run']], env=env, capture_output=True, text=True, check=True)
            return json.loads(result.stdout.splitlines()[0])

    def test_dispatch_forwards_changelog_options_without_shell_expansion(self):
        self.assertEqual(self.dispatch('G2', create_pr='false'), [
            'workflow', 'run', 'member-release.yml', '-R', 'linuxdeepin/go-lib', '--ref', 'release/stable',
            '-f', 'version=6.0.50', '-f', 'name=Release Maintainer $(printf injected)',
            '-f', 'email=release+test@example.org', '-f', 'base_branch=release/stable',
            '-f', 'distribution=stable', '-f', 'create_pr=false'])

    def test_dispatch_g1_only_forwards_supported_inputs(self):
        self.assertEqual(self.dispatch('G1'), [
            'workflow', 'run', 'member-release.yml', '-R', 'linuxdeepin/go-lib', '--ref', 'release/stable',
            '-f', 'version=6.0.50', '-f', 'name=Release Maintainer $(printf injected)',
            '-f', 'email=release+test@example.org', '-f', 'timezone=Asia/Shanghai'])


if __name__ == '__main__':
    unittest.main()
