import tempfile
import unittest
from pathlib import Path
from mulnx_preflight import inspect, read_binding


class MulNXPreflightTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        (root/'CS2Injector').mkdir()
        (root/'CS2Injector'/'CS2Injector.exe').write_bytes(b'fixture-not-executable')
        camera = root/'CS2OBTool'/'CameraSystem'
        (camera/'Config').mkdir(parents=True)
        (camera/'Config'/'Config.yaml').write_text('config:\n  solutions:\n    shortcutEnable: true\n')
        pack = camera/'CamPacks'/'test-map'
        (pack/'CamMacros').mkdir(parents=True)
        config = {}
        for name, prefix, key in [('CT','hlae_spawn_ct','n'),('T','hlae_spawn_t','m'),('ASIDE','hlae_bomb_a','1'),('BSIDE','hlae_bomb_b','6')]:
            (pack/'CamMacros'/f'{name}.yaml').write_text(f'name: {name}\nKCP:\n  usable: true\n  ctrl: false\n  shift: false\n  alt: true\n  vkCode: {ord(key.upper())}\n  comboClick: 2\nsize: 1\n')
            config[prefix+'_key']='alt+'+key
            config[prefix+'_mode']='double'
        return root, pack, config

    def test_matching_files_are_read_only_and_not_real_game_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root, pack, config=self.fixture(directory)
            before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
            result=inspect(root,pack,config)
            self.assertTrue(result['matched'])
            self.assertFalse(result['real_game_confirmed'])
            self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_reset_binding_disabled_and_missing_file_never_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root, pack, config=self.fixture(directory)
            macro=pack/'CamMacros'/'CT.yaml'
            macro.write_text(macro.read_text().replace('comboClick: 2','comboClick: 1'))
            self.assertEqual(inspect(root,pack,config)['scenes']['CT']['state'],'mismatch')
            macro.unlink()
            self.assertEqual(inspect(root,pack,config)['scenes']['CT']['state'],'unknown')
            switch=root/'CS2OBTool'/'CameraSystem'/'Config'/'Config.yaml'
            switch.write_text(switch.read_text().replace('true','false'))
            self.assertFalse(inspect(root,pack,config)['matched'])
            self.assertFalse(inspect(root,pack,config)['shortcut_enabled'])

    def test_other_installation_and_ambiguous_yaml_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, pack, config=self.fixture(directory)
            with self.assertRaises(ValueError):
                inspect(root,root.parent,config)
            macro=pack/'CamMacros'/'CT.yaml'
            macro.write_text(macro.read_text()+'KCP:\n  usable: true\n')
            with self.assertRaises(ValueError):read_binding(macro)
            self.assertEqual(inspect(root,pack,config)['scenes']['CT']['state'],'unknown')
