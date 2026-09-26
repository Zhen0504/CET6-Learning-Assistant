"""Local GUI acceptance fixture: no external AI calls or production learning data.

Run with --module writing|translation. Stop with Ctrl+C to remove fixture data.
"""
import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CN = '虽然电子商务给消费者带来了极大的便利，但它也使传统零售商面临越来越大的竞争压力。'
BAD = "With the development of technology, people's life become more convenient."
TRANSFERS = [
    {'source_cn': '虽然人工智能能够提高工作效率，但它也可能给部分职业带来新的挑战。',
     'reference': 'Although artificial intelligence can improve work efficiency, it may also bring new challenges to some occupations.'},
    {'source_cn': '尽管公共交通的扩展减少了居民的通勤时间，但城市仍需解决偏远地区出行不便的问题。',
     'reference': 'Although the expansion of public transport has reduced commuting time, cities still need to address travel difficulties in remote areas.'},
]


def load_app(module):
    folder = ROOT / ('写作' if module == 'writing' else '翻译')
    sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location('preview_app', folder / 'app.py')
    app_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app_module
    spec.loader.exec_module(app_module)
    return app_module


def fixtures(module):
    if module == 'writing':
        exercise = {'topic': '技术与生活', 'prompt': 'Directions: Write an essay on technology. You should write at least 150 words but no more than 200 words.', 'sample_essay': 'Technology provides people with convenient access to education and information. ' * 15, 'prompt_type': 'direct-topic', 'outline': ['讨论技术的便利与局限'], 'key_phrases': ['access to information（获取信息）']}
        error = {'source_text': BAD, 'issue_type': '主谓一致', 'tag': 'subject_verb_agreement', 'explanation': '复数主语和名词形式应保持一致。', 'suggestion': "With the development of technology, people's lives have become more convenient.", 'severity': 'major'}
        scores = {'task': 78, 'coherence': 76, 'vocab': 75, 'grammar': 70}
        descriptions = {'task_achievement':'基本切题。','coherence':'论证可以更加具体。','vocab':'用词基本准确。','grammar':'注意主谓一致。','errors':['主谓一致需要加强。']}
    else:
        exercise = {'topic': '电子商务', 'source': CN, 'reference': 'Although e-commerce has brought great convenience to consumers, it has also exposed traditional retailers to increasing competitive pressure.', 'key_terms': [{'cn':'电子商务','en':'e-commerce','note':'常用术语'}], 'sentence_notes':[{'source': CN,'analysis':'保留让步和转折关系。'}]}
        error = {'source_cn': CN, 'user_text': 'Although online shopping is convenient, but stores face pressure.', 'issue_type': '长句结构', 'tag': 'long_sentence', 'explanation': 'Although 和 but 不应在同一句中共同连接两个分句。', 'suggestion': exercise['reference'], 'severity': 'major'}
        scores = {'accuracy':78,'fluency':75,'term_use':82,'grammar':70}
        descriptions = {'accuracy':'基本表达原意。','fluency':'部分搭配需调整。','term_use':'术语基本准确。','grammar':'注意让步结构。','missed_terms':[]}
    result = {'score':78,'level':'中等','subscores':scores,'strengths':['主要观点清楚。'],'feedback':'练习长句组织，关注主谓关系。','error_items':[error],'next_focus':['句子结构','语法准确性'],**descriptions}
    return exercise,result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--module', choices=['writing','translation'], required=True)
    args = parser.parse_args()
    module = load_app(args.module)
    exercise,result = fixtures(args.module)
    calls = 0

    def fake_ai(messages, model=None):
        nonlocal calls
        system = messages[0]['content']
        user = messages[-1]['content']
        if '"items"' in system:
            return json.dumps({'items':[{**t,'focus':result['error_items'][0]['tag']} for t in TRANSFERS]}, ensure_ascii=False)
        if 'remaining_issues' in system:
            failed = 'FAIL' in user
            return json.dumps({'score':60 if failed else 88,'passed':not failed,'feedback':'存在严重语法错误。' if failed else '表达自然，目标结构使用正确。','remaining_issues':[{'explanation':'目标结构仍有严重错误','severity':'major'}] if failed else [],'corrected_translation':'Although technology brings convenience, it can also create new challenges.'},ensure_ascii=False)
        if '"subscores"' in system:
            calls += 1
            return json.dumps({**result,'score':72 if calls == 1 else 78},ensure_ascii=False)
        return json.dumps(exercise,ensure_ascii=False)

    module.call_deepseek = fake_ai
    module.get_key = lambda: 'fixture-only'
    if args.module == 'writing':
        module.refine_vocab = lambda exercise, model=None: []
    data_root = ROOT / 'my' / 'learning'
    data_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gui-acceptance-', dir=data_root) as temp:
        module.MY_DIR = Path(temp)
        store = module.learning_tracker
        store.my_dir = Path(temp)
        store.root = Path(temp) / 'learning'
        store.history_path = store.root / (args.module + '_history.jsonl')
        store.queue_path = store.root / (args.module + '_review_queue.json')
        print('FIXTURE DATA:', temp, flush=True)
        module.app.run(host='127.0.0.1',port=5560 if args.module == 'writing' else 5559,debug=False,use_reloader=False)


if __name__ == '__main__':
    main()
