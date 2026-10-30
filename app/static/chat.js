/* 提交方聊天：fetch 流式读取 SSE，打字机 + 状态 + 引用来源 */
(function () {
  var messagesEl = document.getElementById('messages');
  var inputEl = document.getElementById('chatInput');
  var sendBtn = document.getElementById('sendBtn');
  var sessionId = (function () { try { return localStorage.getItem('chat_session') || ''; } catch (e) { return ''; } })();
  var streaming = false;

  function scrollBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }

  function addUserBubble(text) {
    var wrap = document.createElement('div');
    wrap.className = 'msg user';
    var av = document.createElement('div');
    av.className = 'avatar me'; av.textContent = '我';
    var bubble = document.createElement('div');
    bubble.className = 'bubble';
    var t = document.createElement('div'); t.className = 'bubble-text'; t.textContent = text;
    bubble.appendChild(t); wrap.appendChild(av); wrap.appendChild(bubble);
    messagesEl.appendChild(wrap); scrollBottom();
  }

  function addBotBubble() {
    var wrap = document.createElement('div');
    wrap.className = 'msg';
    var av = document.createElement('div');
    av.className = 'avatar bot'; av.textContent = '审';
    var bubble = document.createElement('div');
    bubble.className = 'bubble';
    var t = document.createElement('div'); t.className = 'bubble-text';
    bubble.appendChild(t); wrap.appendChild(av); wrap.appendChild(bubble);
    messagesEl.appendChild(wrap); scrollBottom();
    return {
      append: function (s) { t.textContent += s; scrollBottom(); },
      setText: function (s) { t.textContent = s; scrollBottom(); },
      cite: function (list) {
        var c = document.createElement('div');
        c.className = 'citations';
        c.innerHTML = '<div class="cap">依据的审核规则</div>' + list.map(function (x) {
          return '<div class="cite"><span class="file">' + x.file + '</span>' +
            '<span class="score">片段#' + x.chunk + ' · 相关度 ' + x.score + '</span>' +
            '<div class="snip">' + x.snippet + '</div></div>';
        }).join('');
        bubble.appendChild(c); scrollBottom();
      }
    };
  }

  var statusEl = null;
  function showStatus(text) {
    if (!statusEl) {
      statusEl = document.createElement('div');
      statusEl.className = 'status-line';
      statusEl.innerHTML = '<span class="dots"><span></span><span></span><span></span></span><span class="stext"></span>';
      messagesEl.appendChild(statusEl);
    }
    statusEl.querySelector('.stext').textContent = text || '正在匹配审核规则…';
    scrollBottom();
  }
  function hideStatus() { if (statusEl) { statusEl.remove(); statusEl = null; } }

  function handleFrame(frame, bot, onFirstDelta) {
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
    } else if (event === 'status') {
      showStatus(obj.text);
    } else if (event === 'delta') {
      onFirstDelta();
      bot.append(obj.text || '');
    } else if (event === 'citations') {
      bot.cite(obj || []);
    } else if (event === 'error') {
      hideStatus();
      bot.append('\n[错误 ' + obj.code + '] ' + obj.message);
    }
  }

  async function send() {
    if (streaming) return;
    var q = inputEl.value.trim();
    if (!q) { window.toast && window.toast('请输入内容后再发送'); return; }
    inputEl.value = '';
    streaming = true; sendBtn.disabled = true;

    addUserBubble(q);
    var bot = addBotBubble();
    showStatus('正在匹配审核规则…');

    var first = true;
    function onFirstDelta() { if (first) { first = false; hideStatus(); } }

    try {
      var resp = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId || null, question: q })
      });
      if (resp.status === 401) { location.href = '/login'; return; }
      if (!resp.ok) {
        var err = await resp.json().catch(function () { return {}; });
        hideStatus();
        bot.setText('[错误 ' + (err.code || resp.status) + '] ' + (err.message || '请求失败'));
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
          var frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          handleFrame(frame, bot, onFirstDelta);
        }
      }
      hideStatus();
    } catch (e) {
      hideStatus();
      bot.append('\n[网络异常] ' + e.message);
    } finally {
      streaming = false; sendBtn.disabled = false; inputEl.focus();
    }
  }

  sendBtn.onclick = send;
  inputEl.addEventListener('keydown', function (e) { if (e.key === 'Enter') send(); });
})();
