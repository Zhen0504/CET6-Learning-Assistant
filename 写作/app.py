# -*- coding: utf-8 -*-
"""
CET-6 写作 (Writing) —— Flask 后端
运行：python app.py   （然后自动打开 http://127.0.0.1:5560）
"""

import io
import json
import os
import re
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import requests
from learning_tracking import install_learning, LearningConflict
from flask import Flask, jsonify, render_template, request

import vocab  # 四六级大纲词表加载 + 超纲词检测（本目录 vocab.py）

BASE_DIR = Path(__file__).resolve().parent
MY_DIR = BASE_DIR.parent / "my"
ENV_FILE = BASE_DIR / ".env"

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

DEFAULT_PORT = 5560
TYPE_LABEL = "写作"

app = Flask(__name__)


def load_env(path):
    if not path.exists():
        return
    with io.open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(ENV_FILE)

DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")


def get_key():
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


MODELS_URL = DEEPSEEK_URL.rsplit("/v1/chat/completions", 1)[0] + "/models"
_MODELS_CACHE = {"expires": 0.0, "key": None, "payload": None}
_MODELS_LOCK = threading.Lock()

def _models_fallback(warning):
    configured = os.environ.get("DEEPSEEK_MODEL", "").strip()
    ids = []
    for model_id in (configured, DEFAULT_MODEL, "deepseek-flash"):
        if model_id and model_id not in ids:
            ids.append(model_id)
    return {"models": [{"id": model_id, "name": model_id} for model_id in ids],
            "default_model": DEFAULT_MODEL, "source": "fallback", "warning": warning}

def fetch_models():
    key = get_key()
    now = time.time()
    with _MODELS_LOCK:
        if _MODELS_CACHE["payload"] is not None and _MODELS_CACHE["key"] == key and _MODELS_CACHE["expires"] > now:
            return _MODELS_CACHE["payload"]
    if not key:
        payload = _models_fallback("未配置 DEEPSEEK_API_KEY，已使用默认模型。")
    else:
        try:
            response = requests.get(MODELS_URL, headers={"Authorization": "Bearer " + key}, timeout=15)
            if not response.ok:
                raise ValueError("status %s" % response.status_code)
            raw = response.json()
            rows = raw.get("data") if isinstance(raw, dict) else None
            ids = [row.get("id") for row in rows or [] if isinstance(row, dict) and isinstance(row.get("id"), str) and row.get("id").strip()]
            ids = list(dict.fromkeys(ids))
            if not ids:
                raise ValueError("empty model list")
            if DEFAULT_MODEL not in ids:
                ids.insert(0, DEFAULT_MODEL)
            payload = {"models": [{"id": model_id, "name": model_id} for model_id in ids],
                       "default_model": DEFAULT_MODEL, "source": "remote"}
        except Exception:
            app.logger.warning("model list request failed", exc_info=True)
            payload = _models_fallback("无法获取实时模型列表，已使用默认模型。")
    with _MODELS_LOCK:
        _MODELS_CACHE.update({"expires": time.time() + 600, "key": key, "payload": payload})
    return payload


PROMPTS_DIR = BASE_DIR / "prompts"


def _read_text_file(path):
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def load_gen_prompt():
    t = _read_text_file(PROMPTS_DIR / "system_writing.txt").strip()
    return t if t else "你是 CET-6 写作命题专家。生成 Directions 题干 + 150–200 词范文 + outline + key_phrases，严格只输出 JSON。"


def load_eval_prompt():
    t = _read_text_file(PROMPTS_DIR / "system_evaluate_writing.txt").strip()
    return t if t else "你是 CET-6 写作评分专家。按 14 档评分，严格只输出 JSON。"


class ApiError(Exception):
    def __init__(self, message, status=500):
        super().__init__(message)
        self.message = message
        self.status = status


def safe_text(resp):
    try:
        j = resp.json()
        return (j.get("error") or {}).get("message") or json.dumps(j, ensure_ascii=False)[:300]
    except ValueError:
        return resp.text[:300]


def call_deepseek(messages, model=None):
    key = get_key()
    if not key:
        raise ApiError("服务端未配置 DEEPSEEK_API_KEY。请在 写作/.env 中设置 DEEPSEEK_API_KEY=sk-...", 503)
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
        "max_tokens": 6000,
        "stream": False,
    }
    try:
        resp = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json=payload, timeout=120,
        )
    except requests.RequestException as e:
        raise ApiError("无法连接 DeepSeek（网络问题）：%s" % e, 502)
    if resp.status_code == 401:
        raise ApiError("DeepSeek API Key 无效 (401)，请检查 .env 中的 DEEPSEEK_API_KEY。", 401)
    if resp.status_code == 429:
        raise ApiError("请求过于频繁或额度不足 (429)，请稍后重试。", 429)
    if resp.status_code == 422:
        raise ApiError("DeepSeek 参数有误 (422)：%s" % safe_text(resp), 422)
    if not resp.ok:
        raise ApiError("DeepSeek 请求失败 (%s)：%s" % (resp.status_code, safe_text(resp)), 502)
    try:
        data = resp.json()
    except ValueError:
        raise ApiError("DeepSeek 返回非 JSON。", 502)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ApiError("DeepSeek 返回结构异常：%s" % json.dumps(data, ensure_ascii=False)[:500], 502)


def parse_json_loose(raw):
    s = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, re.I)
    if fence:
        s = fence.group(1).strip()
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b != -1 and b > a:
        s = s[a:b + 1]
    return json.loads(s)


# ----------------------------------------------------------------------
# 词汇合规：扫描超纲词并交 DeepSeek 裁定替换（克隆自听力专项训练）
# ----------------------------------------------------------------------
SYS_VOCAB = (
    "你是 CET-6 英文语料的词汇合规审定员。下面给出若干段英文文本，以及一个由程序用"
    "《四六级大纲词表》自动扫描得出的『疑似超纲词』候选列表。\n"
    "重要：大纲词表并不完整，许多合法的六级词（如 workplace / teamwork / digitization / childhood）"
    "未必被收录。你必须逐个判断：\n"
    " - 若该词其实是常见或六级水平词汇（哪怕词表漏收），请保留，不要替换。\n"
    " - 仅当某词确实生僻、超出六级范围时，才替换为意思最接近的『六级内』同义词；"
    "若单字替换不自然则改写整句。\n"
    "严格要求：\n"
    " 1. 只修订『确实超纲』的词所在句，其余原句一字不改；不得增删整段、不得改变段数与顺序。\n"
    " 2. 必须保持原文原意与全部信息点（人物/事件/数字/因果/观点/态度/术语）。\n"
    " 3. 若文本含 __26__、__27__ 这类空格标记，必须原样保留标记与编号，不得改动。\n"
    " 4. 修订后须自然、连贯，难度维持六级水平。\n"
    " 5. 严格只输出一个 JSON 对象：\n"
    ' {"blocks":["修订后的文本段1","修订后的文本段2",...](段数须与输入完全一致),'
    '"replaced":[{"from":"原超纲词","to":"替换为的大纲词或改写说明","reason":"为何替换"}]}\n'
    "若没有任何词需要替换，blocks 原样返回、replaced 返回空数组 []。"
)


def refine_blocks(blocks, model=None):
    """blocks: list[str] 英文文本段。扫描疑似超纲词，交 DeepSeek 裁定替换。
    返回 (new_blocks_or_None, replaced_list)。词表缺失或无候选返回 (None, [])。"""
    vocab_set, _ = vocab.load_vocab()
    if not vocab_set or not blocks:
        return None, []
    candidates = vocab.scan_out_of_scope("\n".join(blocks), vocab_set)
    if not candidates:
        return None, []
    cand_str = ", ".join(sorted({c["word"] for c in candidates}))
    user_msg = (
        "疑似超纲词候选（小写，已去重）：%s\n\n"
        "待审定文本段（JSON 数组，共 %d 段）：\n%s\n\n"
        "请逐词裁定并输出修订结果（blocks 段数必须与输入一致）。"
        % (cand_str, len(blocks), json.dumps(blocks, ensure_ascii=False))
    )
    try:
        raw = call_deepseek(
            [{"role": "system", "content": SYS_VOCAB}, {"role": "user", "content": user_msg}],
            model,
        )
        data = parse_json_loose(raw)
    except ApiError:
        raise
    except Exception as e:  # noqa: broad-except
        app.logger.warning("vocab refine deepseek failed: %s", e)
        return None, [{"from": c["word"], "to": "", "reason": "扫描到疑似超纲词，自动修订未完成"} for c in candidates]
    if not isinstance(data, dict):
        return None, []
    new_blocks = data.get("blocks")
    replaced = data.get("replaced") or []
    if not isinstance(new_blocks, list) or len(new_blocks) != len(blocks):
        return None, replaced
    if not all(isinstance(b, str) and b.strip() for b in new_blocks):
        return None, replaced
    return new_blocks, replaced


def refine_vocab(exercise, model=None):
    """写作：审定 sample_essay 范文；不动 prompt/outline/key_phrases。"""
    new_blocks, replaced = refine_blocks([exercise.get("sample_essay", "")], model)
    if new_blocks is not None:
        exercise["sample_essay"] = new_blocks[0]
    return replaced




def get_vocab_status(enabled, checked=None):
    try:
        available = bool(vocab.load_vocab()[0])
    except Exception:
        available = False
    if checked is None:
        checked = bool(enabled and available)
    return {
        "enabled": bool(enabled),
        "available": available,
        "checked": bool(checked),
        "warning": "未找到四六级大纲词表，词汇合规检查已跳过。" if enabled and not available else "",
    }


def validate_exercise(d):
    errs = []
    if not isinstance(d, dict):
        raise ValueError("返回不是对象")
    if not isinstance(d.get("prompt"), str) or "Directions" not in d["prompt"]:
        errs.append("prompt 缺失或未含 Directions 题干")
    if not isinstance(d.get("sample_essay"), str) or len(d["sample_essay"].strip()) < 50:
        errs.append("sample_essay 缺失或过短")
    if not isinstance(d.get("outline"), list):
        d["outline"] = []
    if not isinstance(d.get("key_phrases"), list):
        d["key_phrases"] = []
    if errs:
        raise ValueError("；".join(errs))


def build_gen_user(topic):
    t = topic.strip() if topic and topic.strip() else "（请从真题四类模板随机出题：importance of… / saying 谚语 / why students should… / 时代话题）"
    return ("请生成一道 CET-6 写作题：Directions 题干(含 \"at least 150 words but no more than 200 words\")、"
            "150–200 词议论文范文(sample_essay)、outline(中文要点)、key_phrases(高级表达中英对照)。\n"
            "主题/方向：%s\n输出 JSON。" % t)


def build_eval_user(exercise, user_essay):
    return (
        "【写作题干】\n%s\n\n【学习者作文】\n%s\n\n请按考纲写作评分档次评估学习者作文。"
        % (exercise.get("prompt", ""), user_essay or "(空)")
    )


def write_to_my(exercise, label="写作", with_time=False):
    MY_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    base = now.strftime("%y%m%d-%H%M%S") if with_time else now.strftime("%y%m%d")
    path = MY_DIR / (base + label + ".txt")
    n = 1
    while path.exists():
        path = MY_DIR / ("%s-%d%s.txt" % (base, n, label))
        n += 1
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(exercise, ensure_ascii=False, indent=2))
    return path


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "hasKey": bool(get_key()), "model": DEFAULT_MODEL})


@app.route("/api/models")
def api_models():
    return jsonify(fetch_models())


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json(silent=True) or {}
    topic = data.get("topic") or ""
    model = data.get("model") or None
    user_msg = build_gen_user(topic)
    exercise = None
    reminder = ""
    last_err = "未知错误"
    for _attempt in range(2):
        try:
            raw = call_deepseek(
                [{"role": "system", "content": load_gen_prompt()}, {"role": "user", "content": user_msg + reminder}],
                model,
            )
            exercise = parse_json_loose(raw)
            validate_exercise(exercise)
            break
        except ApiError as e:
            return jsonify({"error": e.message}), e.status
        except (ValueError, json.JSONDecodeError) as e:
            last_err = str(e)
            reminder = ("\n\n【修正要求】上次的输出有问题（%s）。请重新生成并严格只输出一个 JSON 对象，"
                        "含 prompt(Directions 题干)、sample_essay(150–200 词范文)、outline、key_phrases。" % last_err)
    else:
        return jsonify({"error": "生成未达标（%s），请再试一次。" % last_err}), 502

    exercise["type"] = "writing"
    exercise["type_label"] = TYPE_LABEL

    # 词汇合规：扫描超纲词并替换（默认开启；前端 vocabCheck 或环境变量 CET_VOCAB_CHECK=0 可关闭）
    exercise["vocab_adjustments"] = []
    vocab_check = data.get("vocabCheck")
    if vocab_check is None:
        vocab_check = os.environ.get("CET_VOCAB_CHECK", "1") not in ("0", "false", "False", "")
    exercise["vocab_status"] = get_vocab_status(vocab_check)
    if vocab_check:
        try:
            replaced = refine_vocab(exercise, model)
            if replaced:
                exercise["vocab_adjustments"] = replaced
        except ApiError:
            raise
        except Exception as e:  # noqa: broad-except
            app.logger.warning("vocab refine skipped: %s", e)

    saved = None
    try:
        saved = str(write_to_my(exercise, label="写作生成", with_time=True).relative_to(BASE_DIR.parent))
    except Exception as e:  # noqa: broad-except
        app.logger.warning("auto-save generation log failed: %s", e)
    return jsonify({"exercise": exercise, "saved": saved})


@app.route("/api/evaluate", methods=["POST"])
def api_evaluate():
    data = request.get_json(silent=True) or {}
    exercise = data.get("exercise")
    user_essay = data.get("answer") or ""
    model = data.get("model") or None
    if not isinstance(exercise, dict):
        return jsonify({"error": "缺少练习数据"}), 400
    try:
        validate_exercise(exercise)
    except ValueError as e:
        return jsonify({"error": "练习数据无效：%s" % e}), 400
    if not isinstance(user_essay, str):
        return jsonify({"error": "答案必须是文本"}), 400
    user_msg = build_eval_user(exercise, user_essay)
    def evaluate_once():
        raw = call_deepseek([{"role": "system", "content": load_eval_prompt()}, {"role": "user", "content": user_msg}], model)
        return parse_json_loose(raw)
    try:
        output = learning_tracker.evaluate(exercise, user_essay, model, data.get("attempt_id"), evaluate_once)
    except LearningConflict as e:
        return jsonify({"error": str(e)}), 409
    except ApiError as e:
        return jsonify({"error": e.message}), e.status
    except (ValueError, TypeError) as e:
        return jsonify({"error": "AI 评分结果或请求无法解析：%s" % e}), 502
    n = len(user_essay.split()) if user_essay.strip() else 0
    output["result"].setdefault("word_count", n)
    output["word_count_server"] = n
    return jsonify(output)


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.get_json(silent=True) or {}
    exercise = data.get("exercise")
    if not isinstance(exercise, dict):
        return jsonify({"error": "缺少练习数据"}), 400
    try:
        path = write_to_my(exercise, label="写作", with_time=False)
        return jsonify({"ok": True, "path": str(path.relative_to(BASE_DIR.parent))})
    except OSError as e:
        return jsonify({"error": "保存失败：%s" % e}), 500


learning_tracker = install_learning(
    app, "writing", MY_DIR, lambda messages, model: call_deepseek(messages, model),
    parse_json_loose, PROMPTS_DIR, ApiError,
)


def open_browser(port):
    webbrowser.open("http://127.0.0.1:%d" % port)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    host = os.environ.get("HOST", "127.0.0.1")
    if not os.environ.get("NO_OPEN_BROWSER"):
        threading.Timer(1.2, lambda: open_browser(port)).start()
    print("=" * 56)
    print("  CET-6 写作  ->  http://%s:%d" % (host, port))
    print("  API Key 状态：" + ("已配置 [OK]" if get_key() else "未配置 [X]  请编辑 .env"))
    print("  按 Ctrl+C 退出")
    print("=" * 56)
    app.run(host=host, port=port, debug=False)
