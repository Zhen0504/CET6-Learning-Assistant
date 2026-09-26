'use strict';

const Learning = (() => {
    const escape = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const value = n => typeof n === 'number' && Number.isFinite(n) ? n : '—';
    const delta = n => typeof n === 'number' && Number.isFinite(n) ? (n > 0 ? '↑ +' : n < 0 ? '↓ ' : '') + n : '数据不足';
    const list = (items, labels = {}) => Array.isArray(items) && items.length ? '<ul>' + items.map(x => '<li>' + escape(labels[x] || x) + '</li>').join('') + '</ul>' : '<p>无</p>';
    let selected = null;
    let items = [];
    let busy = false;
    let reviewRequest = 0;
    const drafts = new Map();
    const draftKey = (kind,index) => JSON.stringify([selected.review_id,kind,index]);
    const requestIds = new Map();
    let lastAttempt = null;
    const uuid = () => globalThis.crypto && crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const n = Math.floor(Math.random()*16); return (c === 'x' ? n : (n & 3) | 8).toString(16); });

    async function api(url, body) {
        const response = await fetch(url, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '请求失败，请重试');
        return data;
    }

    async function attempt(exercise, answer, model) {
        const payload = JSON.stringify({exercise, answer, model});
        if (lastAttempt && lastAttempt.payload === payload) return lastAttempt.id;
        if (!globalThis.crypto || !crypto.subtle) {
            if (!lastAttempt || lastAttempt.payload !== payload) lastAttempt = {payload, id:uuid()};
            return lastAttempt.id;
        }
        const bytes = new TextEncoder().encode(payload);
        const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
        const key = 'cet6-attempt-' + APP_DIR;
        let previous;
        try { previous = JSON.parse(sessionStorage.getItem(key)); } catch (_) {}
        const id = previous && previous.hash === hash ? previous.id : uuid();
        lastAttempt = {payload,id};
        try { sessionStorage.setItem(key, JSON.stringify({hash, id})); } catch (_) {}
        return id;
    }

    function resetAttempt() {
        lastAttempt = null;
        try { sessionStorage.removeItem('cet6-attempt-' + APP_DIR); } catch (_) {}
    }

    function comparison(data) {
        const host = document.getElementById('progressContent');
        const p = data.progress;
        let content = '<h2>📈 与上次相比</h2>';
        if (data.tracking_warning) content += '<p class="learning-warning">' + escape(data.tracking_warning) + '</p>';
        if (!p) content += '<p>本次学习记录暂不可用，评分结果仍可查看。</p>';
        else if (p.is_first_attempt) content += '<p>这是第一条学习记录，完成更多练习后即可查看进步趋势。</p>';
        else {
            content += '<p class="learning-score">本次：' + value(p.current_score) + '　上次：' + value(p.previous_score) + '　' + delta(p.score_delta) + '</p>';
            content += '<div class="learning-table-wrap"><table class="learning-table"><thead><tr><th>分项</th><th>上次</th><th>本次</th><th>变化</th></tr></thead><tbody>';
            for (const [key, label] of Object.entries(data.dimensions || {})) content += '<tr><td>' + escape(label) + '</td><td>' + value((p.previous_subscores || {})[key]) + '</td><td>' + value((p.current_subscores || {})[key]) + '</td><td>' + delta((p.subscore_delta || {})[key]) + '</td></tr>';
            content += '</tbody></table></div><div class="eval-grid">' + [['✅ 本次未再出现',p.resolved_tags],['⚠️ 连续出现',p.repeated_tags],['🆕 本次新问题',p.new_tags]].map(([title,tags]) => '<div class="eval-card"><h5>' + title + '</h5>' + list(tags,data.tag_labels) + '</div>').join('') + '</div><p class="hint">未再出现不代表已经掌握；分数差异也会受到题目难度影响。</p>';
        }
        content += '<div class="eval-card learning-focus"><h5>🎯 下一阶段重点</h5>' + list((data.result || {}).next_focus) + '</div>';
        if (data.history_id) content += '<p class="hint">已自动记录本次评分；可定位的错句默认次日复习。</p>';
        host.innerHTML = content;
        host.classList.remove('hidden');
    }

    function chart(canvas, records, key, label) {
        const ctx = canvas.getContext('2d');
        const width = 640, height = 180;
        canvas.width = width; canvas.height = height;
        ctx.font = '13px sans-serif'; ctx.fillStyle = '#667085';
        for (const n of [0,50,100]) {
            const y = 145 - n * 1.15;
            ctx.fillText(String(n), 4, y + 4);
            ctx.strokeStyle = '#e9ecef'; ctx.beginPath(); ctx.moveTo(36,y); ctx.lineTo(622,y); ctx.stroke();
        }
        ctx.strokeStyle = '#667eea'; ctx.lineWidth = 2; ctx.beginPath();
        let connected = false;
        records.forEach((r,i) => {
            const n = key === 'score' ? r.score : (r.subscores || {})[key];
            if (typeof n !== 'number' || !Number.isFinite(n)) { connected = false; return; }
            const x = records.length === 1 ? 329 : 48 + i * 562 / Math.max(1,records.length - 1), y = 145 - n * 1.15;
            if (connected) ctx.lineTo(x,y); else ctx.moveTo(x,y);
            connected = true;
        });
        ctx.stroke();
        records.forEach((r,i) => {
            const n = key === 'score' ? r.score : (r.subscores || {})[key];
            if (typeof n !== 'number' || !Number.isFinite(n)) return;
            const x = records.length === 1 ? 329 : 48 + i * 562 / Math.max(1,records.length - 1), y = 145 - n * 1.15;
            ctx.fillStyle = '#764ba2'; ctx.beginPath(); ctx.arc(x,y,4,0,Math.PI*2); ctx.fill();
            ctx.fillStyle = '#344054'; ctx.fillText(String(n),x-8,Math.max(15,y-9));
            ctx.fillStyle = '#667085'; ctx.fillText(String(i+1),x-4,168);
        });
        canvas.setAttribute('aria-label',label + '：' + records.map(r => value(key === 'score' ? r.score : (r.subscores || {})[key])).join(' → '));
    }

    async function progress() {
        const data = await api('/api/progress');
        const recent = data.recent || [], counts = data.review_counts || {};
        document.getElementById('learningMessage').textContent = data.warning || '';
        document.getElementById('learningStats').innerHTML = [['总练习',data.total,'次'],['今日待复训',counts.due,'句'],['学习中',counts.learning,'句'],['已掌握',counts.mastered,'句']].map(([label,n,unit]) => '<div class="eval-card"><h5>' + label + '</h5><strong>' + value(n) + '</strong> ' + unit + '</div>').join('');
        const host = document.getElementById('trendContent');
        if (!recent.length) host.innerHTML = '<p>还没有学习记录。完成一次 AI 评分后，记录会自动保存在本地。</p>';
        else if (recent.length < 3) {
            const rows = [['score','总分'], ...Object.entries(data.dimensions || {})];
            host.innerHTML = '<p>' + (recent.length === 1 ? '只有 1 次记录，无法判断趋势。' : '只有 2 次记录，只显示两次差值，不推断长期趋势。') + '</p>' +
                '<div class="learning-table-wrap"><table class="learning-table"><thead><tr><th>指标</th><th>' + (recent.length === 1 ? '当前' : '上次') + '</th>' + (recent.length === 2 ? '<th>本次</th><th>变化</th>' : '') + '</tr></thead><tbody>' +
                rows.map(([key,label]) => { const prev = key === 'score' ? recent[0].score : (recent[0].subscores || {})[key]; const current = recent.length === 2 ? (key === 'score' ? recent[1].score : (recent[1].subscores || {})[key]) : null; const change = current == null || prev == null ? '数据不足' : delta(current - prev); return '<tr><td>' + escape(label) + '</td><td>' + value(prev) + '</td>' + (recent.length === 2 ? '<td>' + value(current) + '</td><td>' + change + '</td>' : '') + '</tr>'; }).join('') + '</tbody></table></div>';
        } else {
            host.innerHTML = '<p>最近最多 10 次记录，按练习先后排列。</p>';
            for (const [key,label] of Object.entries({score:'总分',...(data.dimensions || {})})) {
                const section = document.createElement('section'); section.className = 'learning-trend';
                const heading = document.createElement('h3'); heading.textContent = label;
                const canvas = document.createElement('canvas'); canvas.setAttribute('role','img');
                const text = document.createElement('p'); text.className = 'hint'; text.textContent = recent.map(r => value(key === 'score' ? r.score : (r.subscores || {})[key])).join(' → ');
                section.append(heading,canvas,text); host.append(section); chart(canvas,recent,key,label);
            }
        }
        const improve = data.improvement;
        document.getElementById('improvementContent').innerHTML = improve ? '<p>前 3 次平均 ' + value(improve.previous_average) + ' → 最近 3 次平均 ' + value(improve.recent_average) + '，变化 ' + delta(improve.delta) + '</p>' + Object.entries(improve.subscores || {}).map(([key,n]) => '<p>' + escape((data.dimensions || {})[key] || key) + '：' + value(n.previous_average) + ' → ' + value(n.recent_average) + '（' + delta(n.delta) + '）</p>').join('') : '<p>数据不足：至少完成 6 次练习后，才比较前 3 次与最近 3 次平均。</p>';
        const statuses = {learning:'学习中',mastered:'已掌握',relearning:'重新复习'};
        const occurrences = {first:'首次出现',repeated:'连续出现',recurrence:'曾标记已掌握，之后再次出现',seen:'曾经出现'};
        document.getElementById('errorsContent').innerHTML = (data.frequent_errors || []).length ? data.frequent_errors.map(e => '<div class="eval-card learning-error"><h5>' + escape((data.tag_labels || {})[e.tag] || e.tag) + ' · ' + value(e.count) + ' 次</h5><p>最近 ' + value(e.recent_total) + ' 次出现 ' + value(e.recent_count) + ' 次</p><p>' + escape(occurrences[e.occurrence] || '') + ' · 状态：' + escape(statuses[e.status] || e.status) + '</p></div>').join('') : '<p>暂无结构化错误记录。</p>';
        const history = await api('/api/history');
        const histories = Array.isArray(history.items) ? history.items.slice().reverse() : [];
        document.getElementById('historyContent').innerHTML = histories.length ? histories.map(h => '<details class="learning-history"><summary>' + escape(new Date(h.created_at).toLocaleString()) + ' · ' + escape(h.topic || '练习') + ' · ' + value(h.score) + ' 分</summary><p class="hint">记录 ID：' + escape(h.id) + '</p><h4>题目</h4><p class="learning-pre">' + escape(h.prompt || h.source) + '</p><h4>你的答案</h4><p class="learning-pre">' + escape(h.user_answer) + '</p><h4>评分反馈</h4><p class="learning-pre">' + escape(h.feedback) + '</p>' + (h.error_items || []).map(e => '<div class="eval-card learning-error"><p>' + escape(e.source_text || e.source_cn) + '</p><p>' + escape(e.explanation) + '</p><p>' + escape(e.suggestion) + '</p></div>').join('') + '</details>').join('') : '<p>暂无记录。</p>';
    }

    async function reviews() {
        const version = ++reviewRequest;
        const all = document.getElementById('allReviews').checked;
        const data = await api('/api/review' + (all ? '?all=1' : ''));
        if (version !== reviewRequest) return;
        items = data.items || [];
        document.getElementById('learningMessage').textContent = data.warning || '';
        const host = document.getElementById('reviewList');
        host.innerHTML = items.length ? '' : '<p>当前没有' + (all ? '错句' : '到期错句') + '。新错句默认次日复训，也可勾选“全部错题”提前练习。</p>';
        items.forEach(item => {
            const button = document.createElement('button'); button.className = 'learning-review-choice';
            button.textContent = (item.tag_label || item.tag) + ' · 第 ' + item.stage + '/3 阶段 · ' + (item.status === 'mastered' ? '已掌握' : '复习日 ' + new Date(item.due_at).toLocaleDateString()) + '\n' + item.original_source;
            button.onclick = () => { if (!busy) { selected = item; renderReview(); } };
            host.append(button);
        });
    }

    function resultHTML(result) {
        if (!result) return '';
        return '<div class="reference-box"><h4>' + value(result.score) + ' 分 · ' + (result.passed ? '通过' : '仍需练习') + '</h4><p>' + escape(result.feedback) + '</p>' + list((result.remaining_issues || []).map(x => typeof x === 'string' ? x : x.explanation || x.issue || x.message || '仍有问题')) + '<p>' + escape(result.corrected_translation || result.reference || '') + '</p></div>';
    }

    function answerForm(kind,index) {
        return '<form class="learning-answer-form" data-kind="' + kind + '" data-index="' + index + '"><label class="field-label">' + (kind === 'original' && document.body.dataset.module === 'writing' ? '请重新改写成正确、自然的英文' : '请写出自然、准确的英文') + '<textarea class="write-area learning-answer" required placeholder="在这里输入你的英文答案"></textarea></label><button class="btn btn-orange" type="submit">提交' + (kind === 'transfer' ? '迁移句 ' + (index+1) : '原错句') + '</button></form>';
    }

    function renderReview() {
        const host = document.getElementById('reviewWorkspace');
        if (!selected) { host.classList.add('hidden'); return; }
        host.classList.remove('hidden');
        const round = selected.round;
        let html = '<h2>' + (document.body.dataset.module === 'writing' ? '你之前写错的句子' : '原中文错句') + '</h2><div class="source-box">' + escape(selected.original_source) + '</div><p class="hint">先独立作答，再完成两道迁移训练。提前练习不会推进间隔复习阶段。</p><details id="previousError"><summary>查看上次错误</summary><div id="previousErrorContent">展开后加载。</div></details>';
        if (round && round.completed) {
            html += '<div class="reference-box"><h4>本轮' + (round.passed ? '通过' : '未通过') + '</h4><p>' + (round.early && round.passed ? '这是提前练习，阶段和复习日保持不变。' : '已保存本轮结果与复习安排。') + '</p><p>当前阶段：' + selected.stage + '/3 · ' + (selected.status === 'mastered' ? '已掌握' : '下次复习：' + escape(new Date(selected.due_at).toLocaleString())) + '</p></div><button id="newRound" class="btn btn-primary">再次练习原句</button>';
        } else {
            html += round && round.original ? resultHTML(round.original) : answerForm('original',0);
            if (round && round.original) {
                if (!round.original.passed) html += '<p class="learning-warning">原句未通过，本轮不能通过。结算后将在次日继续复习。</p><button id="completeRound" class="btn btn-primary">结束本轮并安排复习</button>';
                else if (!(round.transfers || []).length) html += '<p class="hint">原句已通过，接下来检验能否用于不同内容。</p><button id="generateTransfer" class="btn btn-primary">生成迁移训练（2 句）</button>';
                else {
                    round.transfers.forEach((item,i) => { html += '<section class="learning-transfer"><h3>迁移训练 ' + (i+1) + '</h3><div class="source-box">' + escape(item.source_cn) + '</div>' + (item.result ? resultHTML(item.result) : answerForm('transfer',i)) + '</section>'; });
                    if (round.transfers.length === 2 && round.transfers.every(x => x.result)) html += '<button id="completeRound" class="btn btn-primary">完成本轮复训</button>';
                }
            }
        }
        host.innerHTML = html;
        host.querySelector('#previousError').addEventListener('toggle',async event => {
            if (!event.target.open) return;
            const content = host.querySelector('#previousErrorContent');
            if (content.dataset.loaded) return;
            try { const previous = await api('/api/review/' + encodeURIComponent(selected.review_id) + '/previous'); content.textContent = '上次答案：' + (previous.old_answer || '未可靠定位') + '\n错误说明：' + (previous.explanation || '无'); content.dataset.loaded = '1'; } catch (error) { content.textContent = error.message; }
        });
        host.querySelectorAll('form').forEach(form => {
            const key = draftKey(form.dataset.kind,Number(form.dataset.index));
            const input = form.querySelector('textarea');
            input.value = drafts.get(key) || '';
            input.addEventListener('input',() => drafts.set(key,input.value));
            form.addEventListener('submit',event => { event.preventDefault(); submitReview(form); });
        });
        const generate = document.getElementById('generateTransfer');
        if (generate) generate.onclick = () => runAction('/api/review/generate-transfer',{});
        const complete = document.getElementById('completeRound');
        if (complete) complete.onclick = () => runAction('/api/review/complete',{});
        const next = document.getElementById('newRound');
        if (next) next.onclick = () => {
            if (busy) return;
            for (const key of requestIds.keys()) if (JSON.parse(key)[0] === selected.review_id) requestIds.delete(key);
            ['original','transfer'].forEach(kind => [0,1].forEach(index => drafts.delete(draftKey(kind,index))));
            selected = {...selected,round:null}; renderReview();
        };
    }

    async function submitReview(form) {
        const answer = form.querySelector('textarea').value.trim();
        if (!answer || busy) return;
        const kind = form.dataset.kind, index = Number(form.dataset.index);
        const key = JSON.stringify([selected.review_id,selected.round && selected.round.round_id,kind,index,answer]);
        if (!requestIds.has(key)) requestIds.set(key,uuid());
        await runAction('/api/review/evaluate',{kind,index,answer,attempt_id:requestIds.get(key)});
    }

    async function runAction(url,extra) {
        if (busy) return;
        busy = true;
        document.getElementById('allReviews').disabled = true;
        const host = document.getElementById('reviewWorkspace');
        host.querySelectorAll('button,textarea').forEach(button => button.disabled = true);
        const msg = document.getElementById('learningMessage');
        msg.textContent = '正在处理，请稍候…';
        try {
            const data = await api(url,{review_id:selected.review_id,round_id:selected.round ? selected.round.round_id : undefined,model:document.getElementById('reviewModel').value || null,...extra});
            selected = data.item;
            renderReview();
            await reviews();
            msg.textContent = data.warning || '已保存。';
        } catch (error) { msg.textContent = error.message; }
        finally { busy = false; document.getElementById('allReviews').disabled = false; host.querySelectorAll('button,textarea').forEach(button => button.disabled = false); }
    }

    document.addEventListener('DOMContentLoaded',async () => {
        const page = document.body.dataset.learningPage;
        if (!page) return;
        try {
            if (page === 'progress') await progress();
            else {
                document.getElementById('allReviews').addEventListener('change',() => { if (!busy) reviews().catch(error => document.getElementById('learningMessage').textContent = error.message); });
                await reviews();
            }
        } catch (error) { document.getElementById('learningMessage').textContent = error.message; }
    });
    return {attempt,resetAttempt,comparison};
})();
