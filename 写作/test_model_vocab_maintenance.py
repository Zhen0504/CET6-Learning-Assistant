import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
MODULES = ['写作', '翻译', '听力专项训练', '选词填空', '长篇阅读', '精读训练', '仔细阅读']
VOCAB_MODULES = ['写作', '听力专项训练', '选词填空', '长篇阅读', '精读训练', '仔细阅读']


def load_app(folder, model=None):
    name = 'maintenance_' + folder
    old = sys.modules.get('learning_tracking')
    old_path = list(sys.path)
    sys.path.insert(0, str(ROOT / folder))
    if folder in ('写作', '翻译'):
        tracker_path = ROOT / folder / 'learning_tracking.py'
        tracker_spec = importlib.util.spec_from_file_location('learning_tracking', tracker_path)
        tracker = importlib.util.module_from_spec(tracker_spec)
        sys.modules['learning_tracking'] = tracker
        tracker_spec.loader.exec_module(tracker)
    old_env = os.environ.get('DEEPSEEK_MODEL')
    if model is None:
        os.environ.pop('DEEPSEEK_MODEL', None)
    else:
        os.environ['DEEPSEEK_MODEL'] = model
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / folder / 'app.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if old_env is None:
            os.environ.pop('DEEPSEEK_MODEL', None)
        else:
            os.environ['DEEPSEEK_MODEL'] = old_env
        if old is None:
            sys.modules.pop('learning_tracking', None)
        else:
            sys.modules['learning_tracking'] = old
        sys.path[:] = old_path


class ModelAndVocabMaintenanceTests(unittest.TestCase):
    def test_default_model_without_env(self):
        for folder in MODULES:
            module = load_app(folder)
            self.assertEqual(module.DEFAULT_MODEL, 'deepseek-flash', folder)

    def test_env_model_is_loaded_before_default_constant(self):
        for folder in MODULES:
            module = load_app(folder, 'test-model')
            self.assertEqual(module.DEFAULT_MODEL, 'test-model', folder)

    def test_models_endpoint_success_and_cache(self):
        for folder in MODULES:
            module = load_app(folder)
            module.get_key = lambda: 'key'
            response = Mock(ok=True)
            response.json.return_value = {'data': [{'id': 'model-a'}, {'id': 'model-b'}]}
            with patch.object(module.requests, 'get', return_value=response) as request:
                first = module.app.test_client().get('/api/models')
                second = module.app.test_client().get('/api/models')
            self.assertEqual(first.status_code, 200, folder)
            self.assertEqual(first.get_json()['source'], 'remote')
            self.assertEqual([x['id'] for x in first.get_json()['models']], ['deepseek-flash', 'model-a', 'model-b'])
            self.assertEqual(first.get_json(), second.get_json())
            request.assert_called_once()

    def test_models_endpoint_fallback(self):
        for folder in MODULES:
            module = load_app(folder)
            module.get_key = lambda: 'key'
            with patch.object(module.requests, 'get', side_effect=OSError('timeout')):
                payload = module.app.test_client().get('/api/models').get_json()
            self.assertEqual(payload['source'], 'fallback', folder)
            self.assertEqual(payload['models'][0]['id'], 'deepseek-flash', folder)
            self.assertTrue(payload['warning'])

    def test_health_contract_remains(self):
        module = load_app('翻译')
        payload = module.app.test_client().get('/api/health').get_json()
        self.assertTrue(set(('ok', 'hasKey', 'model')) <= set(payload))

    def test_missing_vocab_status_does_not_block_generation(self):
        for folder in VOCAB_MODULES:
            module = load_app(folder)
            module.get_key = lambda: 'key'
            exercise = {'passage': 'A sufficiently long passage for careful reading.'}
            if folder == '写作':
                exercise = {'prompt': 'Directions: Write an essay.', 'sample_essay': 'A sample essay that is long enough for validation. ' * 3, 'outline': [], 'key_phrases': []}
            elif folder == '仔细阅读':
                exercise = {'passage': 'A passage that is long enough for validation. ' * 4, 'questions': [{'stem': 'Q', 'options': {'A':'a','B':'b','C':'c','D':'d'}, 'answer':'A'}]}
            elif folder == '选词填空':
                exercise = {'passage': 'A __26__ passage that is long enough.', 'word_bank': [{'letter': chr(65+i), 'word':'word'} for i in range(15)], 'answers': {str(26+i): 'A' for i in range(10)}}
            elif folder == '长篇阅读':
                exercise = {'passage': [{'label':'A','text':'A paragraph long enough.'}], 'statements':[{'text':'s'} for _ in range(10)], 'answers': {str(36+i): 'A' for i in range(10)}}
            elif folder == '精读训练':
                exercise = {'paragraphs':[{'en':'This paragraph is long enough for validation.', 'reference':'参考译文', 'key_terms':[]} for _ in range(4)]}
            else:
                exercise = {'dialogue':[{'speaker':'N','content':'A transcript long enough for validation.'}], 'questions':[{'question':'Q','options':{'A':'a','B':'b','C':'c','D':'d'},'answer':'A'}]}
            module.parse_json_loose = lambda raw, exercise=exercise: exercise
            if hasattr(module, 'validate_exercise'):
                module.validate_exercise = lambda data: None
            if hasattr(module, 'validate_article'):
                module.validate_article = lambda data: None
            module.call_deepseek = lambda messages, model=None: '{}'
            with patch.object(module.vocab, 'load_vocab', return_value=(None, None)):
                # Exercise status helper is the direct, side-effect-free contract used by generation.
                status = module.get_vocab_status(True)
            self.assertFalse(status['available'], folder)
            self.assertFalse(status['checked'], folder)
            self.assertIn('未找到', status['warning'], folder)


if __name__ == '__main__':
    unittest.main()
