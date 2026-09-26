"""Local learning records and server-authoritative sentence review."""
import copy
import hashlib
import json
import logging
import math
import os
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path
from statistics import mean

from flask import jsonify, render_template, request

LABELS = dict(zip(
    'subject_verb_agreement article tense singular_plural preposition clause non_finite sentence_structure word_choice collocation chinese_english coherence task_response punctuation other omission addition mistranslation subject_error logic_relation passive_active long_sentence proper_noun culture_loaded_term number_unit'.split(),
    '主谓一致 冠词 时态 单复数 介词 从句 非谓语 句子结构 词义误选 搭配 中式表达 衔接 切题 标点 其他 漏译 增译 误译 主语错误 逻辑关系 主被动 长句结构 专有名词 文化负载词 数字单位'.split()))
TAGS = {
    'writing': 'subject_verb_agreement article tense singular_plural preposition clause non_finite sentence_structure word_choice collocation chinese_english coherence task_response punctuation other'.split(),
    'translation': 'omission addition mistranslation subject_error logic_relation tense article singular_plural preposition clause non_finite passive_active long_sentence word_choice collocation chinese_english proper_noun culture_loaded_term number_unit other'.split(),
}
DIMENSIONS = {'writing': {'task':'切题', 'coherence':'结构', 'vocab':'词汇', 'grammar':'语法'},
              'translation': {'accuracy':'准确性', 'fluency':'流畅性', 'term_use':'术语', 'grammar':'语法'}}


def text(value):
    return value if isinstance(value, str) else ''


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100 else None


def strings(value):
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def expand_to_full_sentence(full_source, fragment):
    """Expand an exact error fragment to its surrounding sentence."""
    if not isinstance(full_source, str) or not isinstance(fragment, str) or not fragment or fragment not in full_source:
        return None
    start_at = full_source.find(fragment)
    end_at = start_at + len(fragment)
    boundaries = '。！？!?'
    left = max((full_source.rfind(mark, 0, start_at) for mark in boundaries), default=-1) + 1
    right = end_at
    if full_source[end_at - 1] not in boundaries:
        right_candidates = [pos for mark in boundaries if (pos := full_source.find(mark, end_at)) != -1]
        right = min(right_candidates) + 1 if right_candidates else len(full_source)
    return full_source[left:right]


def stamp():
    return datetime.now(timezone.utc).isoformat()


def plus_days(date, days):
    return (datetime.fromisoformat(date) + timedelta(days=days)).isoformat()


class LearningConflict(ValueError):
    pass


class LearningStore:
    def __init__(self, module, my_dir, call_ai, parse_ai, prompt_dir, api_error=None):
        self.module = module
        self.my_dir = Path(my_dir)
        self.root = self.my_dir / 'learning'
        self.history_path = self.root / (module + '_history.jsonl')
        self.queue_path = self.root / (module + '_review_queue.json')
        self.call_ai, self.parse_ai = call_ai, parse_ai
        self.prompt_dir = Path(prompt_dir)
        self.lock = threading.RLock()
        self.now = stamp
        self.logger = logging.getLogger(__name__)
        self.cache = {}

    @staticmethod
    def _atomic(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('w', encoding='utf-8', newline='\n') as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _history(self):
        if not self.history_path.exists():
            self._atomic(self.history_path, '')
        records, warnings = [], []
        for line in self.history_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row.get('type') != self.module or not isinstance(row.get('error_items'), list) or not isinstance(row.get('subscores'), dict):
                    raise ValueError('invalid record')
                records.append(row)
            except (ValueError, TypeError):
                warnings.append('历史文件含损坏记录，已跳过；为保留原数据，暂不写入历史。')
        return records, warnings

    def _queue(self):
        if not self.queue_path.exists():
            self._atomic(self.queue_path, '[]\n')
        raw = self.queue_path.read_text(encoding='utf-8')
        data = json.loads(raw) if raw.strip() else []
        if not isinstance(data, list):
            raise ValueError('复训队列结构损坏，未覆盖原文件')
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get('review_id'), str) or item.get('module') != self.module or type(item.get('stage')) is not int or not 0 <= item['stage'] <= 3 or item.get('status') not in ('learning', 'mastered') or not isinstance(item.get('attempts'), list) or not isinstance(item.get('rounds'), list):
                raise ValueError('复训队列记录损坏，未覆盖原文件')
            datetime.fromisoformat(item['due_at'])
        return data

    def _write_queue(self, queue):
        self._atomic(self.queue_path, json.dumps(queue, ensure_ascii=False, indent=2, allow_nan=False) + '\n')

    @staticmethod
    def _hash(payload):
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

    @staticmethod
    def _attempt_id(value):
        return str(uuid.UUID(value)) if value else str(uuid.uuid4())

    def _clean_errors(self, exercise, answer, result):
        raw = result.get('error_items')
        errors = []
        if not isinstance(raw, list):
            return errors
        key = 'source_text' if self.module == 'writing' else 'source_cn'
        source = answer if self.module == 'writing' else text(exercise.get('source'))
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            error = {k: text(entry.get(k)) for k in (key, 'issue_type', 'explanation', 'suggestion')}
            error['tag'] = entry.get('tag') if entry.get('tag') in TAGS[self.module] else 'other'
            error['severity'] = 'major' if entry.get('severity') == 'major' else 'minor'
            error['source_valid'] = bool(error[key].strip()) and error[key] in source
            if self.module == 'translation' and error['source_valid']:
                expanded = expand_to_full_sentence(source, error[key])
                if expanded:
                    error[key] = expanded
                    error['source_expanded'] = expanded != text(entry.get('source_cn'))
            if self.module == 'translation':
                user_text = text(entry.get('user_text'))
                error['user_text'] = user_text if user_text in answer else ''
            errors.append(error)
        return errors

    def _record(self, exercise, answer, result, attempt_id, request_hash):
        writing = self.module == 'writing'
        fields = ('prompt', 'prompt_type', 'sample_essay') if writing else ('source', 'reference')
        record = {key: text(exercise.get(key)) for key in ('topic',) + fields}
        record.update(id=str(uuid.uuid4()), created_at=self.now(), type=self.module,
                      attempt_id=attempt_id, request_hash=request_hash, user_answer=answer,
                      score=number(result.get('score')), level=text(result.get('level')),
                      subscores={key: number((result.get('subscores') or {}).get(key)) for key in DIMENSIONS[self.module]} if isinstance(result.get('subscores'), dict) else {},
                      feedback=text(result.get('feedback')), strengths=strings(result.get('strengths')),
                      next_focus=strings(result.get('next_focus')), error_items=self._clean_errors(exercise, answer, result))
        for key in (('task_achievement', 'coherence', 'vocab', 'grammar') if writing else ('accuracy', 'fluency', 'term_use', 'grammar')):
            record[key] = text(result.get(key))
        list_key = 'errors' if writing else 'missed_terms'
        record[list_key] = strings(result.get(list_key))
        if writing:
            record['word_count'] = len(answer.split())
        return record

    def _sync_queue(self, records):
        queue = self._queue()
        known = {x['review_id'] for x in queue}
        changed = False
        for record in records:
            for index, error in enumerate(record['error_items']):
                if not isinstance(error, dict) or not error.get('source_valid') or not text(error.get('suggestion')).strip():
                    continue
                key = 'source_text' if self.module == 'writing' else 'source_cn'
                original = text(error.get(key))
                source = text(record.get('user_answer' if self.module == 'writing' else 'source'))
                if not original.strip() or original not in source:
                    continue
                review_id = str(uuid.uuid5(uuid.NAMESPACE_URL, record['id'] + ':' + str(index)))
                if review_id in known:
                    continue
                tag = error['tag']
                recurrence = any(x['tag'] == tag and x.get('mastered_at') and x['mastered_at'] <= record['created_at'] for x in queue)
                reference_answer = error['suggestion']
                if self.module == 'translation' and error.get('source_expanded'):
                    reference_answer = ''
                queue.append({'review_id':review_id, 'source_history_id':record['id'], 'module':self.module,
                              'tag':tag, 'tag_label':LABELS[tag], 'original_source':original,
                              'old_answer':original if self.module == 'writing' else error.get('user_text', ''),
                              'reference_answer':reference_answer, 'explanation':error['explanation'],
                              'created_at':record['created_at'], 'due_at':plus_days(record['created_at'], 1),
                              'stage':0, 'status':'learning', 'recurrence':recurrence, 'attempts':[], 'rounds':[]})
                known.add(review_id)
                changed = True
        if changed:
            self._write_queue(queue)
        return queue

    def _tags(self, record):
        return {x['tag'] for x in record['error_items'] if isinstance(x, dict) and x.get('tag') in TAGS[self.module]}

    @staticmethod
    def _delta(a, b):
        return round(b-a, 2) if number(a) is not None and number(b) is not None else None

    def compare(self, record, previous):
        if previous is None:
            return {'is_first_attempt':True}
        old, new = self._tags(previous), self._tags(record)
        return {'previous_score':previous['score'], 'current_score':record['score'],
                'score_delta':self._delta(previous['score'],record['score']),
                'previous_subscores':previous['subscores'], 'current_subscores':record['subscores'],
                'subscore_delta':{k:self._delta(previous['subscores'].get(k),record['subscores'].get(k)) for k in DIMENSIONS[self.module]},
                'repeated_tags':sorted(old & new), 'resolved_tags':sorted(old-new), 'new_tags':sorted(new-old)}

    def evaluate(self, exercise, answer, model, attempt_id, evaluator):
        with self.lock:
            attempt_id = self._attempt_id(attempt_id)
            fingerprint = self._hash([exercise, answer, model])
            warnings = []
            try:
                records, warnings = self._history()
            except Exception:
                records = []
                warnings = ['历史读取失败，本次评分仍可正常查看。']
                self.logger.warning('Learning history read failed', exc_info=True)
            previous = None
            for row in records:
                if row.get('attempt_id') == attempt_id:
                    if row.get('request_hash') != fingerprint:
                        raise LearningConflict('attempt_id 已用于不同答案，请开始新的评分尝试')
                    result_keys = ['score','level','subscores','strengths','errors','missed_terms','feedback','error_items','next_focus','task_achievement','coherence','vocab','grammar','accuracy','fluency','term_use','word_count']
                    output = {'result':{k:row[k] for k in result_keys if k in row}, 'history_id':row['id'], 'progress':self.compare(row,previous), 'dimensions':DIMENSIONS[self.module], 'tag_labels':LABELS}
                    try:
                        self._sync_queue(records)
                    except Exception:
                        warnings.append('复训队列暂不可用，历史已保存。')
                        self.logger.warning('Review queue recovery failed', exc_info=True)
                    if warnings:
                        output['tracking_warning'] = '；'.join(set(warnings))
                    return output
                previous = row
            if attempt_id in self.cache:
                cached_hash, cached = self.cache[attempt_id]
                if cached_hash != fingerprint:
                    raise LearningConflict('attempt_id 已用于不同请求')
                return copy.deepcopy(cached)
            # The AI call is part of the scoring path. Only persistence/statistics
            # failures are downgraded to a warning; an AI/API failure must reach Flask.
            result = evaluator()
            if not isinstance(result, dict):
                raise ValueError('AI 评分必须返回 JSON 对象')
            output = {'result':result, 'dimensions':DIMENSIONS[self.module], 'tag_labels':LABELS}
            try:
                if warnings:
                    raise OSError('；'.join(set(warnings)))
                record = self._record(exercise, answer, result, attempt_id, fingerprint)
                self._atomic(self.history_path, ''.join(json.dumps(x, ensure_ascii=False, allow_nan=False)+'\n' for x in records+[record]))
                output.update(history_id=record['id'], progress=self.compare(record,previous))
                self._sync_queue(records+[record])
            except Exception:
                self.logger.warning('Learning tracking failed; AI score retained', exc_info=True)
                output['tracking_warning'] = '学习追踪暂不可用；评分仍已完成。' + ('历史已保存，复训队列待恢复。' if output.get('history_id') else '本次历史未保存，请保留评分结果或手动保存。')
            self.cache[attempt_id] = (fingerprint,copy.deepcopy(output))
            if len(self.cache) > 100:
                del self.cache[next(iter(self.cache))]
            return output

    def record_evaluation(self, exercise, answer, result, attempt_id=None, model=None):
        return self.evaluate(exercise,answer,model,attempt_id,lambda:result)

    def history(self):
        with self.lock:
            records, warnings = self._history()
            return {'items':records, 'warning':'；'.join(set(warnings))}

    def progress(self):
        with self.lock:
            records, warnings = self._history()
            try:
                queue = self._sync_queue(records)
            except Exception:
                self.logger.warning('Review queue unavailable',exc_info=True)
                queue = []
                warnings.append('复训队列暂不可用，复训数量无法统计。')
            counter = Counter(x['tag'] for row in records for x in row['error_items'] if isinstance(x,dict) and x.get('tag') in TAGS[self.module])
            recent = records[-5:]
            frequent = []
            for tag,count in counter.most_common():
                related = [x for x in queue if x['tag'] == tag]
                occurrences = sum(tag in self._tags(x) for x in records)
                recurring = any(x.get('recurrence') for x in related)
                repeated = len(records) >= 2 and all(tag in self._tags(x) for x in records[-2:])
                frequent.append({'tag':tag,'count':count,'recent_count':sum(tag in self._tags(x) for x in recent),'recent_total':len(recent),
                                 'occurrence':'recurrence' if recurring else 'repeated' if repeated else 'first' if occurrences == 1 else 'seen',
                                 'status':'mastered' if related and all(x['status']=='mastered' for x in related) else 'relearning' if recurring else 'learning'})
            def average(rows,key=None):
                values = [x.get('score') if key is None else x['subscores'].get(key) for x in rows]
                return round(mean(values),2) if len(values)==3 and all(number(x) is not None for x in values) else None
            improvement = None
            if len(records) >= 6:
                old,new = records[-6:-3],records[-3:]
                def comparison(key=None):
                    a,b = average(old,key),average(new,key)
                    return {'previous_average':a,'recent_average':b,'delta':self._delta(a,b)}
                improvement = {**comparison(), 'subscores':{k:comparison(k) for k in DIMENSIONS[self.module]}}
            return {'total':len(records),'recent':[{k:x.get(k) for k in ('id','created_at','score','subscores')} for x in records[-10:]],
                    'dimensions':DIMENSIONS[self.module],'tag_labels':LABELS,'frequent_errors':frequent,'improvement':improvement,
                    'review_counts':{'due':sum(x['status']=='learning' and x['due_at']<=self.now() for x in queue),'learning':sum(x['status']=='learning' for x in queue),'mastered':sum(x['status']=='mastered' for x in queue)} if not warnings else {'due':None,'learning':None,'mastered':None},
                    'warning':'；'.join(set(warnings))}

    def _public(self,item):
        output = {k:item[k] for k in ('review_id','tag','tag_label','original_source','created_at','due_at','stage','status')}
        output['round'] = None
        if item['rounds']:
            row = item['rounds'][-1]
            def attempt(value):
                return {k:value[k] for k in ('score','passed','feedback','remaining_issues','corrected_translation','reference') if k in value} if value else None
            output['round'] = {'round_id':row['round_id'], 'original':attempt(row['original']),
                               'transfers':[{'source_cn':x['source_cn'],'focus':x['focus'],'result':attempt(x.get('result'))} for x in row['transfers']],
                               'completed':row['completed'],'passed':row.get('passed'),'early':row.get('early')}
        return output

    def list_review(self,all_items=False):
        with self.lock:
            records,warnings = self._history()
            queue = self._sync_queue(records)
            return {'items':[self._public(x) for x in queue if all_items or (x['status']=='learning' and x['due_at']<=self.now())], 'warning':'；'.join(set(warnings))}

    def _item(self,queue,review_id):
        item = next((x for x in queue if x['review_id']==review_id),None)
        if item is None:
            raise ValueError('错句不存在')
        return item

    @staticmethod
    def _round(item,round_id):
        if not round_id:
            raise ValueError('缺少 round_id')
        row = next((r for r in item['rounds'] if r['round_id']==round_id),None)
        if row is None:
            raise ValueError('复训轮次不存在')
        return row

    def _ai(self,operation,payload,model):
        prompt = (self.prompt_dir / ('system_review_'+operation+'_'+self.module+'.txt')).read_text(encoding='utf-8')
        result = self.parse_ai(self.call_ai([{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}],model))
        if not isinstance(result,dict):
            raise ValueError('AI 返回结构无效')
        return result

    def review_evaluate(self,data):
        with self.lock:
            answer = text(data.get('answer')).strip()
            kind = data.get('kind')
            if not answer or kind not in ('original','transfer'):
                raise ValueError('请提交有效英文答案和训练类型')
            attempt_id = self._attempt_id(data.get('attempt_id'))
            queue = self._queue()
            item = self._item(queue,data.get('review_id'))
            fingerprint = self._hash([item['review_id'],kind,data.get('index',0),answer,data.get('model')])
            for candidate in queue:
                for previous in candidate['attempts']:
                    if previous['attempt_id']==attempt_id:
                        if previous['request_hash']!=fingerprint or (data.get('round_id') and data['round_id']!=previous['round_id']):
                            raise LearningConflict('attempt_id 已用于其他答案或轮次')
                        return {'result':{k:previous[k] for k in ('score','passed','feedback','remaining_issues','corrected_translation')},'item':self._public(item)}
            if kind=='original' and not data.get('round_id'):
                if item['rounds'] and not item['rounds'][-1]['completed']:
                    row = item['rounds'][-1]
                else:
                    row = {'round_id':str(uuid.uuid4()),'created_at':self.now(),'early':item['due_at']>self.now(),'original':None,'transfers':[],'completed':False}
                    item['rounds'].append(row)
            else:
                row = self._round(item,data.get('round_id'))
            if row['completed'] or row is not item['rounds'][-1]:
                raise LearningConflict('该轮已结束，请开始新一轮')
            if kind=='original':
                if row['original']:
                    raise LearningConflict('原句已提交，请继续本轮训练')
                source,reference = item['original_source'],item['reference_answer']
                target = None
            else:
                index = data.get('index')
                if type(index) is not int or index not in (0,1) or len(row['transfers'])!=2 or not row['original'] or not row['original']['passed']:
                    raise ValueError('先通过原句并生成两道迁移题')
                target = row['transfers'][index]
                if target.get('result'):
                    raise LearningConflict('该迁移句已经提交')
                source,reference = target['source_cn'],target['reference']
            result = self._ai('evaluate',{'kind':kind,'source':source,'tag':item['tag'],'explanation':item['explanation'],'reference':reference,'answer':answer},data.get('model'))
            score = number(result.get('score'))
            issues = result.get('remaining_issues')
            if score is None or not isinstance(issues,list) or any(not isinstance(x,dict) or x.get('severity') not in ('major','minor') for x in issues):
                raise ValueError('AI 复训评分缺少有效分数或严重程度，请重试')
            major = any(x['severity']=='major' for x in issues) or result.get('has_major_error') is True
            attempt = {'attempt_id':attempt_id,'request_hash':fingerprint,'round_id':row['round_id'],'created_at':self.now(),
                       'kind':kind,'source_cn':source if kind=='transfer' or self.module=='translation' else '', 'source_text':source if kind=='original' and self.module=='writing' else '',
                       'answer':answer,'score':score,'passed':score>=80 and not major,'has_major_error':major,
                       'feedback':text(result.get('feedback')),'remaining_issues':[{'explanation':text(x.get('explanation')),'severity':x['severity']} for x in issues],
                       'corrected_translation':text(result.get('corrected_translation'))}
            if target is None:
                row['original'] = attempt
            else:
                attempt['reference'] = reference
                target['result'] = attempt
            item['attempts'].append(attempt)
            self._write_queue(queue)
            return {'result':self._public(item)['round']['original'] if kind=='original' else self._public(item)['round']['transfers'][data['index']]['result'],'item':self._public(item)}

    def generate_transfer(self,data):
        with self.lock:
            queue = self._queue()
            item = self._item(queue,data.get('review_id'))
            row = self._round(item,data.get('round_id'))
            if row['completed'] or not row['original'] or not row['original']['passed']:
                raise ValueError('必须先通过本轮原错句')
            if row['transfers']:
                return {'item':self._public(item)}
            result = self._ai('generate',{'original_source':item['original_source'],'tag':item['tag'],'explanation':item['explanation'],'reference':item['reference_answer'], 'previous_sources':[t['source_cn'] for r in item['rounds'] for t in r['transfers']]},data.get('model'))
            generated = result.get('items')
            if not isinstance(generated,list) or len(generated)!=2:
                raise ValueError('迁移题必须恰好两句，请重试')
            existing = [item['original_source']] + [t['source_cn'] for r in item['rounds'] for t in r['transfers']]
            transfers = []
            for entry in generated:
                if not isinstance(entry,dict):
                    raise ValueError('迁移题格式无效')
                source,reference = text(entry.get('source_cn')).strip(),text(entry.get('reference')).strip()
                if not 8<=len(source)<=220 or not reference or entry.get('focus')!=item['tag'] or not any('\u4e00'<=c<='\u9fff' for c in source):
                    raise ValueError('迁移题缺少有效中文句子、参考答案或正确考点')
                if any(SequenceMatcher(None,source,old).ratio()>=0.82 for old in existing):
                    raise ValueError('迁移题与原句或已用句过于相似，请重新生成')
                existing.append(source)
                transfers.append({'source_cn':source,'reference':reference,'focus':item['tag'],'result':None})
            row['transfers'] = transfers
            self._write_queue(queue)
            return {'item':self._public(item)}

    def complete(self,data):
        with self.lock:
            queue = self._queue()
            item = self._item(queue,data.get('review_id'))
            row = self._round(item,data.get('round_id'))
            if row['completed']:
                return {'item':self._public(item),'passed':row['passed'],'early':row['early']}
            if not row['original']:
                raise ValueError('请先提交原句')
            passed = False
            if row['original']['passed']:
                if len(row['transfers'])!=2 or any(not t['result'] for t in row['transfers']):
                    raise ValueError('请完成两道迁移句')
                results = [t['result'] for t in row['transfers']]
                passed = mean(x['score'] for x in results)>=80 and not any(x['has_major_error'] for x in results)
            now = self.now()
            if passed and not row['early']:
                item['stage'] = min(3,item['stage']+1)
                item['status'] = 'mastered' if item['stage']==3 else 'learning'
                item['due_at'] = plus_days(now,3 if item['stage']==1 else 7)
                if item['status']=='mastered':
                    item['mastered_at'] = now
            elif not passed:
                item.update(stage=max(0,item['stage']-1),status='learning',due_at=plus_days(now,1))
            row.update(completed=True,passed=passed,completed_at=now)
            self._write_queue(queue)
            return {'item':self._public(item),'passed':passed,'early':row['early']}

    def previous(self,review_id):
        with self.lock:
            item = self._item(self._queue(),review_id)
            return {'old_answer':item['old_answer'],'explanation':item['explanation']}


def install_learning(app,module,my_dir,call_ai,parse_ai,prompt_dir,api_error=None):
    tracker = LearningStore(module,my_dir,call_ai,parse_ai,prompt_dir,api_error)
    app.extensions['learning_tracking_'+module] = tracker

    def safe(function):
        @wraps(function)
        def wrapped(*args,**kwargs):
            try:
                return jsonify(function(*args,**kwargs))
            except LearningConflict as error:
                return jsonify(error=str(error)),409
            except (ValueError,TypeError) as error:
                app.logger.warning('Learning request rejected: %s',error)
                return jsonify(error=str(error)),400
            except Exception as error:
                app.logger.warning('Learning operation unavailable',exc_info=True)
                if api_error and isinstance(error,api_error):
                    return jsonify(error=error.message),error.status
                return jsonify(error='学习数据暂不可用，请检查本地文件或稍后重试；原评分功能不受影响。'),503
        return wrapped

    for page in ('progress','review'):
        app.add_url_rule('/'+page, 'learning_'+page, lambda page=page:render_template('learning.html',module=module,page=page,label='写作' if module=='writing' else '翻译'))
    app.add_url_rule('/api/history','learning_history',safe(tracker.history))
    app.add_url_rule('/api/progress','learning_progress_api',safe(tracker.progress))
    app.add_url_rule('/api/review','learning_review_api',safe(lambda:tracker.list_review(request.args.get('all')=='1')))
    app.add_url_rule('/api/review/<review_id>/previous','learning_previous',safe(tracker.previous))
    def post(method):
        def handler():
            data = request.get_json(silent=True)
            if not isinstance(data,dict):
                raise ValueError('需要 JSON 对象')
            return method(data)
        return handler
    for path,method in [('evaluate',tracker.review_evaluate),('generate-transfer',tracker.generate_transfer),('complete',tracker.complete)]:
        app.add_url_rule('/api/review/'+path,'learning_'+path,safe(post(method)),methods=['POST'])
    return tracker
