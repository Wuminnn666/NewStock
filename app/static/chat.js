/* 提交方咨询（Agent 版）
 *
 * 相比上一版的差别：
 *   1. 请求打到 /api/agent/run —— 背后是 Agent 执行循环，而不是一次性问答；
 *   2. 新增「执行轨迹」区：把每一轮的 工具名 / 耗时 / 结果摘要 显示出来（可折叠）；
 *   3. 新增「草稿确认卡片」：create_goods_draft 前服务端会强制中断，
 *      草稿内容以 draft_delta 流式送达，用户点「确认创建」后才继续执行。
 *
 * SSE 事件：meta / thinking / tool_call / tool_result / draft_start / draft_delta
 *           / confirm_required / delta / citations / done / error
 */
(function () {
  var messagesEl = document.getElementById('messages');
  var inputEl = document.getElementById('chatInput');
  var sendBtn = document.getElementById('sendBtn');
  var sessionId = (function () { try { return localStorage.getItem('chat_session') || ''; } catch (e) { return ''; } })();
  var streaming = false;
  var pendingConfirm = null;   // 有待确认的草稿时，禁止发起新问题

  var TOOL_CN = {
    search_review_rules: '检索审核规则',
    get_category_requirements: '查询类目资质要求',
    material_validate: '资料完整性校验',
    qualification_audit: '资质审核',
    compliance_check: '合规校验',
    create_goods_draft: '创建商品草稿',
    request_missing_material: '生成待补资料清单'
  };
  var STATUS_CN = { ok: '成功', error: '失败', gate_blocked: '被门禁拦截', skipped: '跳过' };

  function scrollBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function addUserBubble(text) {
    var wrap = el('div', 'msg user');
    var av = el('div', 'avatar me', '我');
    var bubble = el('div', 'bubble');
    bubble.appendChild(el('div', 'bubble-text', text));
    wrap.appendChild(av); wrap.appendChild(bubble);
    messagesEl.appendChild(wrap); scrollBottom();
  }

  // 机器人气泡：轨迹区 + 正文 + 草稿卡 + 引用区
  function addBotBubble() {
    var wrap = el('div', 'msg');
    var av = el('div', 'avatar bot', '审');
    var bubble = el('div', 'bubble');

    var trace = el('div', 'trace');
    var traceHead = el('div', 'trace-head');
    traceHead.innerHTML = '<span class="tcap">执行轨迹</span><span class="tcount">0 步</span><span class="tarrow">▾</span>';
    var traceBody = el('div', 'trace-body');
    trace.appendChild(traceHead); trace.appendChild(traceBody);

    var textEl = el('div', 'bubble-text');
    var draftCard = null, draftBody = null;
    var citeBox = null;

    bubble.appendChild(trace); bubble.appendChild(textEl);
    wrap.appendChild(av); wrap.appendChild(bubble);
    messagesEl.appendChild(wrap); scrollBottom();

    var rows = {};
    return {
      // 轨迹行：先占位（调用中），拿到结果再回填状态
      callTool: function (d) {
        var row = el('div', 'trace-row running');
        row.innerHTML = '<span class="ico">▷</span>' +
          '<span class="tname">第' + d.round + '轮 · ' + (TOOL_CN[d.tool] || d.tool) + '</span>' +
          '<span class="tmeta">执行中…</span>';
        traceBody.appendChild(row);
        rows[d.round + '-' + d.tool] = row;
        traceHead.querySelector('.tcount').textContent = traceBody.children.length + ' 步';
        trace.classList.add('show');
        scrollBottom();
      },
      toolResult: function (d) {
        var row = rows[d.round + '-' + d.tool];
        if (!row) { this.callTool(d); row = rows[d.round + '-' + d.tool]; }
        row.classList.remove('running');
        row.classList.add(d.status);
        row.querySelector('.ico').textContent = d.status === 'ok' ? '✔' : (d.status === 'skipped' ? '–' : '✕');
        var meta = row.querySelector('.tmeta');
        meta.textContent = (STATUS_CN[d.status] || d.status) +
          (d.elapsed_ms ? ' · ' + d.elapsed_ms + 'ms' : '');
        // 摘要折叠在行下方，点行展开
        if (d.summary) {
          var s = el('div', 'trace-sum', d.summary);
          row.appendChild(s);
          row.onclick = function () { row.classList.toggle('open'); };
        }
        scrollBottom();
      },
      draftStart: function () {
        draftCard = el('div', 'draft-card');
        draftCard.appendChild(el('div', 'draft-head', '商品上架草稿 · 待您确认'));
        draftBody = el('pre', 'draft-body');
        draftCard.appendChild(draftBody);
        bubble.insertBefore(draftCard, textEl.nextSibling);
        scrollBottom();
      },
      draftDelta: function (t) { if (draftBody) { draftBody.textContent += t; scrollBottom(); } },
      showConfirm: function (confirmId) {
        if (!draftCard) this.draftStart();
        var actions = el('div', 'draft-actions');
        var ok = el('button', 'btn primary sm', '确认创建草稿');
        var no = el('button', 'btn sm', '取消');
        actions.appendChild(ok); actions.appendChild(no);
        draftCard.appendChild(actions);
        draftCard.classList.add('await');

        pendingConfirm = confirmId;
        setInputEnabled(false);

        ok.onclick = function () { decide(true); };
        no.onclick = function () { decide(false); };

        var self = this;
        function decide(approved) {
          ok.disabled = true; no.disabled = true;
          actions.remove();
          var tip = el('div', 'draft-tip', approved ? '已确认，正在创建草稿…' : '已取消，正在收尾…');
          draftCard.appendChild(tip);
          draftCard.classList.remove('await');
          pendingConfirm = null;
          // 续跑：同一个气泡接着输出（服务端会从断点继续循环）
          self.stream('/api/agent/confirm', { confirm_id: confirmId, approved: approved });
          setInputEnabled(true);
        }
        scrollBottom();
      },
      append: function (s) { textEl.textContent += s; scrollBottom(); },
      setText: function (s) { textEl.textContent = s; scrollBottom(); },
      cite: function (list) {
        citeBox = el('div', 'citations');
        citeBox.innerHTML = '<div class="cap">依据的审核规则</div>' + list.map(function (x) {
          return '<div class="cite"><span class="file">' + x.file + '</span>' +
            '<span class="score">片段#' + x.chunk + ' · 相关度 ' + x.score + '</span>' +
            '<div class="snip">' + x.snippet + '</div></div>';
        }).join('');
        bubble.appendChild(citeBox); scrollBottom();
      },
      // 复用同一个 SSE 解析器：初次提问与「确认后续跑」走同一套逻辑
      stream: async function (url, payload) {
        var resp = await fetch(url, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (resp.status === 401) { location.href = '/login'; return; }
        if (!resp.ok) {
          var err = await resp.json().catch(function () { return {}; });
          hideStatus();
          this.append('\n[错误 ' + (err.code || resp.status) + '] ' + (err.message || '请求失败'));
          return;
        }
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buf = '';
        while (true) {
          var chunk = await reader.read();
          if (chunk.done) break;
          buf += decoder.decode(chunk.value, { stream: true });
          var idx;
          while ((idx = buf.indexOf('\n\n')) >= 0) {
            handleFrame(buf.slice(0, idx), this);
            buf = buf.slice(idx + 2);
          }
        }
        hideStatus();
      }
    };
  }

  // ---------------- 状态条 ----------------
  var statusEl = null;
  function showStatus(text) {
    if (!statusEl) {
      statusEl = el('div', 'status-line');
      statusEl.innerHTML = '<span class="dots"><span></span><span></span><span></span></span><span class="stext"></span>';
      messagesEl.appendChild(statusEl);
    }
    statusEl.querySelector('.stext').textContent = text || '正在匹配审核规则…';
    scrollBottom();
  }
  function hideStatus() { if (statusEl) { statusEl.remove(); statusEl = null; } }

  function setInputEnabled(on) {
    inputEl.disabled = !on;
    sendBtn.disabled = !on;
  }

  // ---------------- SSE 帧解析 ----------------
  var firstDeltaSeen = false;
  function handleFrame(frame, bot) {
    var lines = frame.split('\n');
    var event = 'message', data = '';
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf('event:') === 0) event = lines[i].slice(6).trim();
      else if (lines[i].indexOf('data:') === 0) data += lines[i].slice(5).trim();
    }
    if (!data) return;
    var obj;
    try { obj = JSON.parse(data); } catch (e) { return; }

    if (event === 'meta') {
      sessionId = obj.session_id || sessionId;
      try { localStorage.setItem('chat_session', sessionId); } catch (e) {}
    } else if (event === 'thinking') {
      showStatus('第 ' + obj.round + '/' + obj.max + ' 轮：智能体正在决策…');
    } else if (event === 'tool_call') {
      hideStatus();
      bot.callTool(obj);
    } else if (event === 'tool_result') {
      bot.toolResult(obj);
    } else if (event === 'draft_start') {
      hideStatus();
      bot.draftStart();
      showStatus('正在生成上架草稿，请确认…');
    } else if (event === 'draft_delta') {
      bot.draftDelta(obj.text || '');
    } else if (event === 'confirm_required') {
      hideStatus();
      bot.showConfirm(obj.confirm_id);
    } else if (event === 'delta') {
      if (!firstDeltaSeen) { firstDeltaSeen = true; hideStatus(); }
      bot.append(obj.text || '');
    } else if (event === 'citations') {
      bot.cite(obj || []);
    } else if (event === 'error') {
      hideStatus();
      bot.append('\n[错误 ' + obj.code + '] ' + obj.message);
    }
  }

  // ---------------- 发送 ----------------
  async function send() {
    if (streaming) return;
    if (pendingConfirm) { window.toast && window.toast('请先确认或取消草稿'); return; }
    var q = inputEl.value.trim();
    if (!q) { window.toast && window.toast('请输入内容后再发送'); return; }
    inputEl.value = '';
    streaming = true; sendBtn.disabled = true; firstDeltaSeen = false;

    addUserBubble(q);
    var bot = addBotBubble();
    showStatus('正在理解您的问题…');

    try {
      await bot.stream('/api/agent/run', { session_id: sessionId || null, question: q });
    } catch (e) {
      hideStatus();
      bot.append('\n[网络异常] ' + e.message);
    } finally {
      streaming = false;
      if (!pendingConfirm) setInputEnabled(true);
      inputEl.focus();
    }
  }

  sendBtn.onclick = send;
  inputEl.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.isComposing) send();
  });
})();
