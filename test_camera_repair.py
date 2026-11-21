import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('camera_ui_repair', ROOT / 'deploy/repair_camera_ui.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class CameraRepairTests(unittest.TestCase):
    def test_legacy_page_fragments_and_script_are_fixed_idempotently(self):
        # Keep the actual legacy bug forms as a regression fixture here; tests
        # must not depend on obsolete distribution ZIPs surviving cleanup.
        html = '''const zlmBase = `${location.protocol}//${location.hostname}:8080`;
const canMove = connected && operator && cameraOnline && ptzReady && motionTarget !== "turtlesim";
function sendPtzAxes() { if (!operator || !cameraOnline || !ptzReady || motionTarget === "turtlesim") return false; }
<script src="/static/input-controls.js?v=20261109"></script>'''
        script = "function ptzAllowed() { return operator && cameraOnline && ptzReady && motionTarget!=='turtlesim'; }"
        result = repair.fixed_html(html)
        inputs = repair.fixed_inputs(script)
        self.assertIn('["8090", "8093"].includes(location.port)', result)
        self.assertIn('http://${location.hostname}:8080', result)
        self.assertNotIn('motionTarget', result.split('const canMove =', 1)[1].split(';', 1)[0])
        self.assertNotIn('motionTarget!==' + "'turtlesim'", inputs)
        self.assertIn('operator && cameraOnline && ptzReady', inputs)
        self.assertEqual(repair.fixed_html(result), result)
        self.assertEqual(repair.fixed_inputs(inputs), inputs)

    def test_unrecognized_page_is_not_patched(self):
        for text in ('no endpoint', 'const zlmBase = 1; const zlmBase = 2;', '/api/video/local.mp4'):
            with self.assertRaises(ValueError):
                repair.fixed_html(text)

    def test_existing_local_stream_is_not_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / 'Desktop/ZLMediaKit/release/linux/Debug/config.ini'
            cfg.parent.mkdir(parents=True)
            cfg.write_text('[api]\nsecret=test-secret\n')
            with patch.object(repair, 'call', return_value={'code':0,'data':[{'schema':'rtsp'}]}) as call:
                repair.restore_source(home)
            self.assertTrue(all(c.args[1] == 'getMediaList' for c in call.call_args_list))

    def test_missing_stream_reuses_saved_credentials_without_prompt(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cfg = home / 'Desktop/ZLMediaKit/release/linux/Debug/config.ini'
            cfg.parent.mkdir(parents=True)
            cfg.write_text('[api]\nsecret=test-secret\n')
            saved = home / '.config/hkcamera/stack.json'
            saved.parent.mkdir(parents=True)
            saved.write_text(json.dumps({'camera_password':'a@b:#'}))
            with patch.object(repair, 'call', side_effect=[{'code':0,'data':[]},{'code':0},{'code':0,'data':[{'schema':'rtmp'}]}]) as call:
                repair.restore_source(home)
            self.assertEqual(call.call_args_list[1].args[1], 'addStreamProxy')
            self.assertIn('a%40b%3A%23@', call.call_args_list[1].args[2]['url'])
