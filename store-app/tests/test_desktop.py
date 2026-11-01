"""Headless lifecycle tests; real native-engine smoke checks run on Windows/macOS CI."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

import desktop


class DesktopTests(unittest.TestCase):
    def make_webview(self):
        module = types.SimpleNamespace(settings={}, FileDialog=types.SimpleNamespace(FOLDER=20))
        module.window = MagicMock()
        module.create_window = MagicMock(return_value=module.window)
        module.start = MagicMock()
        return module

    @contextmanager
    def runtime(self, module, occupied=False):
        service = MagicMock()
        probe = MagicMock()
        probe.__enter__.return_value.connect_ex.return_value = 0 if occupied else 1
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict('sys.modules', webview=module), \
                 patch.object(desktop.socket, 'socket', return_value=probe), \
                 patch.object(desktop.server, 'DATA', Path(folder)), \
                 patch.object(desktop.server, 'create_server', return_value=service), \
                 patch.object(desktop.threading, 'Thread'), \
                 patch.object(desktop.server, 'connect') as connect, \
                 patch.object(desktop.server, 'backup') as backup:
                yield service, backup, Path(folder)

    def test_native_window_preserves_profile_and_backs_up_on_close(self):
        webview = self.make_webview()
        with self.runtime(webview) as (service, backup, folder):
            with patch.object(desktop.sys, 'platform', 'win32'):
                desktop.run()
            args, options = webview.create_window.call_args
            self.assertIn('ALI BABA', args[0])
            self.assertEqual(args[1], 'http://127.0.0.1:8765')
            self.assertTrue(options['confirm_close'])
            self.assertTrue(webview.settings['ALLOW_DOWNLOADS'])
            self.assertEqual(webview.start.call_args.kwargs['gui'], 'edgechromium')
            self.assertFalse(webview.start.call_args.kwargs['private_mode'])
            self.assertEqual(webview.start.call_args.kwargs['storage_path'], str(folder / 'webview'))
            service.shutdown.assert_called_once()
            service.server_close.assert_called_once()
            backup.assert_called_once()
            self.assertIsNone(desktop.server.CHOOSE_FOLDER)

    def test_engine_failure_still_stops_server_and_backs_up(self):
        webview = self.make_webview()
        webview.start.side_effect = RuntimeError('WebView2 missing')
        with self.runtime(webview) as (service, backup, _):
            with self.assertRaisesRegex(RuntimeError, 'WebView2'):
                desktop.run()
            service.shutdown.assert_called_once()
            backup.assert_called_once()
            self.assertIsNone(desktop.server.CHOOSE_FOLDER)

    def test_second_launch_does_not_open_browser_or_second_service(self):
        webview = self.make_webview()
        with self.runtime(webview, occupied=True) as (service, backup, _):
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                desktop.run()
            webview.create_window.assert_not_called()
            service.shutdown.assert_not_called()
            backup.assert_not_called()

    def test_native_folder_picker_handles_cancel_and_selection(self):
        webview = self.make_webview()
        def gui_loop(*args, **kwargs):
            webview.window.create_file_dialog.return_value = None
            self.assertEqual(desktop.server.CHOOSE_FOLDER(), '')
            webview.window.create_file_dialog.return_value = ('/Volumes/Backup',)
            self.assertEqual(desktop.server.CHOOSE_FOLDER(), '/Volumes/Backup')
        webview.start.side_effect = gui_loop
        with self.runtime(webview):
            with patch.object(desktop.sys, 'platform', 'darwin'):
                desktop.run()
            self.assertEqual(webview.start.call_args.kwargs['gui'], 'cocoa')


if __name__ == '__main__':
    unittest.main()
