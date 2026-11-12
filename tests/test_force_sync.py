"""Real Git validates leased alignment and preservation of overwritten history."""
import threading
import unittest
from unittest import mock
import test_restore
from app import backups, engine, force_sync
from app.protection import load_review, review_token


class ForceSyncTests(unittest.TestCase):
    git = test_restore.RestoreTests.git
    row = test_restore.RestoreTests.row
    local_runner = test_restore.RestoreTests.local_runner

    def setUp(self):
        test_restore.RestoreTests.setUp(self)
        self.db.run("UPDATE repositories SET mode='force',deletion_max_percent=100 WHERE id=?", (self.repo_id,))
        self.client.file_paths = lambda name, sha: set(self.git(
            '--git-dir=' + str(self.remote), 'ls-tree', '-r', '--name-only', sha).splitlines())
        self.target_head = self.target
        original_get_ref = self.client.get_ref
        self.client.get_ref = lambda name, branch: self.target_head if name == 'up/demo' else original_get_ref(name, branch)
        runner = mock.patch.object(force_sync, '_run_git', side_effect=self.local_runner)
        runner.start(); self.addCleanup(runner.stop)
        fetch = mock.patch.object(force_sync, '_fetch_repository', side_effect=lambda client, name, sha, folder:
            self.local_runner(['fetch', '--no-tags', 'https://github.com/me/demo.git', sha], folder))
        fetch.start(); self.addCleanup(fetch.stop)

    def align(self):
        return force_sync.align_upstream(self.db, self.client, self.row(), self.current, self.target_head,
                                         deletion_guard=engine._deletion_guard)

    def test_rewinds_to_upstream_with_backup_and_exact_lease(self):
        self.assertEqual(self.align(), 'synced')
        self.assertEqual(self.client.get_ref('me/demo', 'main'), self.target)
        backup = self.db.one('SELECT * FROM repository_backups WHERE fork_sha=?', (self.current,))
        self.assertTrue(force_sync.verified_backup(self.db, backup))
        push = next(c for c in self.commands if c[0] == 'push')
        self.assertIn('--force-with-lease=refs/heads/main:' + self.current, push)
        self.assertEqual(self.row()['mode'], 'force')

    def test_backup_failure_never_pushes(self):
        with mock.patch.object(force_sync, 'ensure_backup', return_value={'status': 'error'}):
            self.assertEqual(self.align(), 'paused')
        self.assertFalse(any(c[0] == 'push' for c in self.commands))
        self.assertEqual(self.client.get_ref('me/demo', 'main'), self.current)

    def test_mode_change_during_backup_cancels_alignment(self):
        original = force_sync.ensure_backup
        def backup(*args):
            result = original(*args)
            self.db.run("UPDATE repositories SET mode='protected',enabled=0,status='paused' WHERE id=?", (self.repo_id,))
            return result
        with mock.patch.object(force_sync, 'ensure_backup', side_effect=backup):
            self.assertEqual(self.align(), 'paused')
        self.assertFalse(any(c[0] == 'push' for c in self.commands))
        self.assertEqual(self.row()['mode'], 'protected')

    def test_lease_rejects_concurrent_remote_update(self):
        def concurrent_push():
            self.git('-C', str(self.work), 'commit', '--allow-empty', '-qm', 'concurrent user commit')
            self.git('-C', str(self.work), 'push', 'origin', 'main')
        self.push_hook = concurrent_push
        self.assertEqual(self.align(), 'paused')
        self.assertEqual(self.row()['enabled'], 0)
        self.assertIn('结果未确认', self.row()['last_error'])

    def test_timeout_after_actual_push_is_paused_not_retried(self):
        runner = self.local_runner
        def timeout(args, *a, **kw):
            result = runner(args, *a, **kw)
            if args[0] == 'push':
                raise TimeoutError()
            return result
        with mock.patch.object(force_sync, '_run_git', side_effect=timeout):
            self.assertEqual(self.align(), 'paused')
        self.assertEqual(self.client.get_ref('me/demo', 'main'), self.target)
        self.assertEqual(self.row()['enabled'], 0)

    def test_engine_passes_required_deletion_guard_and_protected_mode_never_aligns(self):
        from test_engine import node
        observation = node('N1', 'me/demo', 'up/demo', self.current, self.target)
        with mock.patch.object(engine, 'align_upstream', return_value='synced') as align:
            self.assertEqual(engine.sync_one(self.db, self.client, self.row(), observation), 'synced')
            align.assert_called_once_with(self.db, self.client, self.row(), self.current, self.target,
                                          deletion_guard=engine._deletion_guard)
        self.db.run("UPDATE repositories SET mode='protected',enabled=0 WHERE id=?", (self.repo_id,))
        with mock.patch.object(engine, 'align_upstream') as align:
            self.assertEqual(engine.sync_one(self.db, self.client, self.row(), observation), 'paused')
            align.assert_not_called()

    def test_enabling_protection_during_fetch_blocks_destructive_target(self):
        self.db.run("UPDATE repositories SET deletion_protection=0,deletion_max_percent=50 WHERE id=?",
                    (self.repo_id,))
        fetch = force_sync._fetch_repository
        def enable_protection(*args):
            result = fetch(*args)
            self.db.run("UPDATE repositories SET deletion_protection=1 WHERE id=?", (self.repo_id,))
            return result
        with mock.patch.object(force_sync, '_fetch_repository', side_effect=enable_protection):
            self.assertEqual(self.align(), 'needs_attention')
        self.assertEqual(self.client.get_ref('me/demo', 'main'), self.current)
        self.assertEqual(load_review(self.row())['deleted_percent'], 100)
        self.assertFalse(any(c[0] == 'push' for c in self.commands))

    def test_protection_change_after_review_cancels_push(self):
        authenticate = force_sync._authenticated_git_env
        def change_policy(client):
            self.db.set_setting('deletion_max_files', '0')
            return authenticate(client)
        with mock.patch.object(force_sync, '_authenticated_git_env', side_effect=change_policy):
            self.assertEqual(self.align(), 'paused')
        self.assertEqual(self.client.get_ref('me/demo', 'main'), self.current)
        self.assertFalse(any(c[0] == 'push' for c in self.commands))

    def test_deleted_baseline_after_review_cancels_push(self):
        guard = engine._deletion_guard
        def delete_after_review(*args):
            allowed = guard(*args)
            record = self.db.one('SELECT * FROM repository_backups WHERE fork_sha=?', (self.current,))
            backups.delete_backup(self.db, record['id'], self.repo_id)
            return allowed
        with mock.patch.object(engine, '_deletion_guard', side_effect=delete_after_review):
            self.assertEqual(self.align(), 'paused')
        self.assertEqual(self.client.get_ref('me/demo', 'main'), self.current)
        self.assertFalse(any(c[0] == 'push' for c in self.commands))

    def test_manual_deletion_waits_until_push_verification_finishes(self):
        started, finished = threading.Event(), threading.Event()
        failures, threads = [], []
        def delete_during_push():
            record = self.db.one('SELECT * FROM repository_backups WHERE fork_sha=?', (self.current,))
            def remove():
                started.set()
                try:
                    backups.delete_backup(self.db, record['id'], self.repo_id)
                except Exception as error:
                    failures.append(error)
                finally:
                    self.db.close()
                    finished.set()
            thread = threading.Thread(target=remove)
            threads.append(thread)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.1))
            self.assertTrue(backups.verified_backup(self.db, record))
        self.push_hook = delete_during_push
        try:
            self.assertEqual(self.align(), 'synced')
        finally:
            for thread in threads:
                thread.join(5)
        self.assertTrue(finished.is_set())
        self.assertEqual(failures, [])

    def test_alignment_transport_can_align_to_empty_upstream_tree_after_review(self):
        remote = '--git-dir=' + str(self.remote)
        empty = self.git(remote, 'hash-object', '-w', '-t', 'tree', '/dev/null')
        self.target_head = self.git(remote, '-c', 'user.name=Upstream', '-c', 'user.email=up@example.invalid',
                                    'commit-tree', empty, '-p', self.current, '-m', 'empty upstream')
        self.git(remote, 'update-ref', 'refs/heads/upstream', self.target_head)
        self.assertEqual(self.align(), 'needs_attention')
        self.assertEqual(self.client.get_ref('me/demo', 'main'), self.current)
        self.db.run('UPDATE repositories SET deletion_approval=? WHERE id=?',
                    (review_token(load_review(self.row())), self.repo_id))
        self.assertEqual(self.align(), 'synced')
        self.assertEqual(self.git(remote, 'ls-tree', 'main'), '')
        self.assertTrue(self.db.one('SELECT id FROM repository_backups WHERE fork_sha=?', (self.current,)))
