// kobito_agents フロントエンド

const API = '/api';
let currentAgentId = null;
let currentSessionId = null;
let agents = [];
let sessionStates = {}; // { sessionId: 'idle' | 'waiting' | 'streaming' }
let sessionModelTiers = {}; // { sessionId: 'deep' | 'quick' }
let activeProcessSessions = new Set(); // 常駐プロセスが稼働中のセッションID
let respondingSessions = new Set();   // バックエンドで応答処理中（ロック取得中）のセッションID
let processStatusInterval = null;
let lastDirMtime = 0;     // セッションディレクトリの前回mtime
let lastWatchingMtime = 0; // 表示中セッションJSONLの前回mtime
let lastStartupId = null;  // サーバー起動IDキャッシュ（変化でリロード検知）

// ============================================================
// 初期化
// ============================================================

document.addEventListener('DOMContentLoaded', async () => {
  await loadAgents();
  initTabs();
  initResize();
  initInput();
  initActions();
});

// ============================================================
// エージェント一覧
// ============================================================

async function loadAgents() {
  const resp = await fetch(`${API}/agents`);
  agents = await resp.json();
  renderAgents();
  if (agents.length > 0) {
    selectAgent(agents[0].id);
  }
}

function renderAgents() {
  const list = document.getElementById('agent-list');
  list.innerHTML = agents.map(a => `
    <div class="agent-item${a.id === currentAgentId ? ' active' : ''}" data-agent-id="${a.id}">
      <div class="agent-avatar">${a.name.charAt(0)}</div>
      <div class="agent-info">
        <div class="agent-name">${a.name}</div>
        <div class="agent-desc">${a.description || ''}</div>
      </div>
    </div>
  `).join('');

  list.querySelectorAll('.agent-item').forEach(el => {
    el.addEventListener('click', () => selectAgent(el.dataset.agentId));
  });
}

async function selectAgent(agentId) {
  currentAgentId = agentId;
  currentSessionId = null;
  renderAgents();
  await loadSessions();
  clearChat();
  loadSettingsData();
  // エージェントのCLI種別に合わせてモデル選択肢を更新
  const agent = agents.find(a => a.id === agentId);
  if (agent) updateModelSelect(agent);
  // プロセスステータスのポーリングを開始
  startProcessStatusPolling();
}

async function pollProcessStatus() {
  if (!currentAgentId) return;
  try {
    let url = `${API}/agents/${currentAgentId}/process-status`;
    if (currentSessionId) url += `?watching=${currentSessionId}`;
    const resp = await fetch(url);
    if (!resp.ok) return;
    const data = await resp.json();

    // サーバー再起動検知
    if (data.startup_id) {
      if (lastStartupId && lastStartupId !== data.startup_id) {
        sessionStates = {};
        showToast('サーバーが再起動されました');
        await loadSessions();
        if (currentSessionId) await loadSessionHistory(currentSessionId);
      }
      lastStartupId = data.startup_id;
    }

    // プロセス稼働ドット更新
    activeProcessSessions = new Set(data.active);
    document.querySelectorAll('.conversation-item').forEach(el => {
      const sid = el.dataset.sessionId;
      el.classList.toggle('process-active', activeProcessSessions.has(sid));
    });

    // バックエンド応答中セッションを確認し、フロントが見逃しているものを補完
    respondingSessions = new Set(data.responding);
    respondingSessions.forEach(sid => {
      if (!sessionStates[sid]) {
        sessionStates[sid] = 'streaming';
        // セッション一覧のインジケーターをDOMに直接追加（再レンダリングなし）
        const el = document.querySelector(`.conversation-item[data-session-id="${sid}"]`);
        if (el && !el.querySelector('.conv-status')) {
          const div = document.createElement('div');
          div.className = 'conv-status';
          div.innerHTML = '<div class="spinner"></div><span class="label">応答待ち</span>';
          el.appendChild(div);
        }
      }
    });

    // セッションディレクトリ変化 → 一覧を再取得
    if (data.dir_mtime && data.dir_mtime !== lastDirMtime) {
      if (lastDirMtime !== 0) await loadSessions();
      lastDirMtime = data.dir_mtime;
    }

    // 表示中セッション変化 → 履歴を再取得（自分がストリーミング中でなければ）
    if (data.watching_mtime && data.watching_mtime !== lastWatchingMtime) {
      if (lastWatchingMtime !== 0 && !sessionStates[currentSessionId]) {
        await loadSessionHistory(currentSessionId);
      }
      lastWatchingMtime = data.watching_mtime;
    }
  } catch (_) {}
}

function startProcessStatusPolling() {
  if (processStatusInterval) clearInterval(processStatusInterval);
  activeProcessSessions = new Set();
  respondingSessions = new Set();
  pollProcessStatus();
  processStatusInterval = setInterval(pollProcessStatus, 5000);
}

// ============================================================
// セッション一覧
// ============================================================

async function loadSessions() {
  if (!currentAgentId) return;
  // フェッチ開始時点のsessionStatesスナップショットを取る（非同期競合対策）
  const stateSnapshot = { ...sessionStates };
  const resp = await fetch(`${API}/agents/${currentAgentId}/sessions`);
  const sessions = await resp.json();
  renderSessions(sessions, stateSnapshot);
}

function renderSessions(sessions, stateSnapshot) {
  const states = stateSnapshot || sessionStates;
  const list = document.getElementById('conversation-list');
  if (sessions.length === 0) {
    list.innerHTML = '<div style="padding:20px;color:var(--text-muted);text-align:center;">会話がありません</div>';
    return;
  }
  list.innerHTML = sessions.map(s => {
    const date = new Date(s.updated_at).toLocaleString('ja-JP', {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
    const state = states[s.session_id];
    const statusHtml = state === 'waiting' || state === 'streaming'
      ? '<div class="conv-status"><div class="spinner"></div><span class="label">応答待ち</span></div>'
      : '';
    const titleHtml = s.title
      ? `<div class="conv-title">${escapeHtml(s.title)}</div>`
      : '';
    const isProcessActive = activeProcessSessions.has(s.session_id);
    return `
      <div class="conversation-item${s.session_id === currentSessionId ? ' active' : ''}${isProcessActive ? ' process-active' : ''}" data-session-id="${s.session_id}">
        <div class="conv-header">
          <span class="conv-date">${date}</span>
          <span class="conv-header-right">
            <span class="conv-count">(${s.message_count})</span>
            <span class="process-dot" title="プロセス稼働中"></span>
          </span>
        </div>
        ${titleHtml}
        <div class="conv-preview">${escapeHtml(s.last_message)}</div>
        ${statusHtml}
      </div>
    `;
  }).join('');

  list.querySelectorAll('.conversation-item').forEach(el => {
    el.addEventListener('click', () => selectSession(el.dataset.sessionId));
  });
}

async function selectSession(sessionId) {
  currentSessionId = sessionId;
  lastWatchingMtime = 0; // セッション切替時にリセット
  // モバイル: チャット画面に切り替え
  if (isMobile()) {
    document.querySelector('.layout').classList.add('mobile-chat-active');
  }
  // アクティブ状態を更新
  document.querySelectorAll('.conversation-item').forEach(el => {
    el.classList.toggle('active', el.dataset.sessionId === sessionId);
  });
  // セッションごとのモデル選択を復元（メモリ→メタデータ→エージェントデフォルトの優先順）
  const agent = agents.find(a => a.id === currentAgentId);
  const sel = document.getElementById('model-select');
  const inMemory = sessionModelTiers[sessionId];
  if (inMemory) {
    sel.value = inMemory;
  } else {
    // セッション一覧からメタデータのmodel_tierを取得
    const resp = await fetch(`${API}/agents/${currentAgentId}/sessions`);
    const sessions = await resp.json();
    const session = sessions.find(s => s.session_id === sessionId);
    const metaTier = session?.model_tier;
    if (metaTier) {
      sel.value = metaTier;
      sessionModelTiers[sessionId] = metaTier;
    } else if (agent) {
      sel.value = agent.model_tier || 'deep';
    }
  }
  applyModelSelectStyle(sel);
  await loadSessionHistory(sessionId);
}

// ============================================================
// チャット履歴
// ============================================================

let currentSessionTitle = '';

async function loadSessionHistory(sessionId) {
  if (!currentAgentId || !sessionId) return;
  const resp = await fetch(`${API}/agents/${currentAgentId}/sessions/${sessionId}`);
  const messages = await resp.json();
  renderMessages(messages);

  // セッション一覧からタイトルを取得
  const sessResp = await fetch(`${API}/agents/${currentAgentId}/sessions`);
  const sessions = await sessResp.json();
  const session = sessions.find(s => s.session_id === sessionId);
  currentSessionTitle = session?.title || '';

  const date = messages.length > 0
    ? new Date(messages[0].timestamp).toLocaleString('ja-JP')
    : '';
  const titleEl = document.getElementById('chat-title');
  titleEl.textContent = currentSessionTitle || (date ? `${date} の会話` : '');
}

function renderMessages(messages) {
  const container = document.getElementById('chat-messages');
  container.innerHTML = '';

  const agent = agents.find(a => a.id === currentAgentId);
  const agentName = agent ? agent.name : '';

  messages.forEach(m => {
    // 空メッセージはスキップ
    const content = (m.content || '').trim();
    if (!content && (!m.tool_uses || m.tool_uses.length === 0)) return;

    // ツール使用通知
    if (m.tool_uses && m.tool_uses.length > 0) {
      m.tool_uses.forEach(tu => {
        const notice = document.createElement('div');
        notice.className = 'tool-use-notice';
        const desc = describeToolUse(tu);
        notice.innerHTML = `<span class="icon">&#9881;</span> ${escapeHtml(desc)}`;
        container.appendChild(notice);
      });
    }

    // 空contentのtool_useのみメッセージはバブルをスキップ
    if (!content) return;

    const div = document.createElement('div');
    div.className = `message ${m.role}`;

    const sender = m.role === 'user' ? 'あなた' : agentName;
    const time = new Date(m.timestamp).toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });

    // Markdownレンダリング（assistantのみ）
    const bubbleContent = m.role === 'assistant' && typeof marked !== 'undefined'
      ? marked.parse(content)
      : escapeHtml(content);

    div.innerHTML = `
      <div class="message-sender">${escapeHtml(sender)}</div>
      <div class="message-bubble">${bubbleContent}</div>
      <div class="message-time">${time}</div>
    `;
    container.appendChild(div);
  });

  container.scrollTop = container.scrollHeight;
}

const MODEL_LABELS = {
  claude: { deep: 'opus', quick: 'sonnet' },
  codex:  { deep: 'o3',   quick: 'o4-mini' },
};

function updateModelSelect(agent) {
  const sel = document.getElementById('model-select');
  const labels = MODEL_LABELS[agent.cli] || MODEL_LABELS.claude;
  sel.innerHTML = Object.entries(labels)
    .map(([tier, label]) => `<option value="${tier}">${label}</option>`)
    .join('');
  sel.value = agent.model_tier || 'deep';
  applyModelSelectStyle(sel);
}

function applyModelSelectStyle(sel) {
  sel.classList.toggle('model-deep', sel.value === 'deep');
  sel.classList.toggle('model-quick', sel.value === 'quick');
}

function clearChat() {
  document.getElementById('chat-messages').innerHTML = '';
  document.getElementById('chat-title').textContent = '';
  currentSessionTitle = '';
}

async function saveSessionTitle(title) {
  if (!currentAgentId || !currentSessionId) return;
  await fetch(`${API}/agents/${currentAgentId}/sessions/${currentSessionId}/title`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
  currentSessionTitle = title;
  document.getElementById('chat-title').textContent = title || '';
  await loadSessions();
}

function describeToolUse(tu) {
  const name = tu.name || '';
  const input = tu.input || {};
  if (input.file_path) return `${name}: ${input.file_path.split(/[/\\]/).pop()}`;
  if (input.command) return `${name}: ${input.command.substring(0, 60)}`;
  if (input.pattern) return `${name}: ${input.pattern}`;
  return name;
}

// ============================================================
// メッセージ送信
// ============================================================

function initInput() {
  const input = document.getElementById('chat-input');
  const sendBtn = document.getElementById('send-btn');

  sendBtn.addEventListener('click', sendMessage);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMessage();
    }
  });

  // モデル変更をセッションごとに記憶・保存
  document.getElementById('model-select').addEventListener('change', async (e) => {
    applyModelSelectStyle(e.target);
    const tier = e.target.value;
    const key = currentSessionId || 'new';
    sessionModelTiers[key] = tier;
    // セッションが確定している場合はメタデータに保存
    if (currentSessionId && currentAgentId) {
      await fetch(`${API}/agents/${currentAgentId}/sessions/${currentSessionId}/model-tier`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_tier: tier }),
      });
    }
  });
}

async function sendMessage() {
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message || !currentAgentId) return;

  input.value = '';

  // 送信時のセッションIDを記憶（別セッションに切り替わっても追跡できるように）
  const sentSessionId = currentSessionId;
  const sentAgentId = currentAgentId;

  // 現在のセッションを表示中かどうか判定するヘルパー
  const isViewingThisSession = () =>
    currentAgentId === sentAgentId && currentSessionId === sentSessionId;

  // ユーザーメッセージを即座に表示
  appendMessage('user', message);

  // 考え中表示
  const thinkingEl = appendThinking();

  // セッション状態を更新
  sessionStates[sentSessionId || 'new'] = 'waiting';
  await loadSessions();

  try {
    const modelTier = document.getElementById('model-select').value;
    const body = { message, session_id: sentSessionId, model_tier: modelTier };
    const resp = await fetch(`${API}/agents/${sentAgentId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    // SSEストリーミング
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let fullResponse = '';
    let newSessionId = null;
    let streamError = null;

    sessionStates[sentSessionId || 'new'] = 'streaming';

    // バブルは最初のチャンクが来た時点で作成する（それまで考え中表示を維持）
    let bubbleEl = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      const text = decoder.decode(value);
      const lines = text.split('\n');
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const event = JSON.parse(line.slice(6));
            if (event.type === 'chunk') {
              fullResponse = event.data;
              if (isViewingThisSession()) {
                const container = document.getElementById('chat-messages');
                if (!bubbleEl) {
                  bubbleEl = appendMessage('assistant', '');
                }
                bubbleEl.querySelector('.message-bubble').textContent = fullResponse;
                // thinkingElを常に末尾に移動してスクロールアウトを防ぐ
                if (thinkingEl.parentNode) container.appendChild(thinkingEl);
              }
            } else if (event.type === 'tool_use') {
              if (isViewingThisSession()) {
                const container = document.getElementById('chat-messages');
                appendToolUse(event.data);
                // tool_use追加後もthinkingElを末尾に移動
                if (thinkingEl.parentNode) container.appendChild(thinkingEl);
              }
            } else if (event.type === 'error') {
              streamError = event.data;
            } else if (event.type === 'session_id') {
              newSessionId = event.data;
              // 新規セッションのモデル選択を引き継ぎ、メタデータに保存
              const newTier = sessionModelTiers['new'];
              if (newTier) {
                sessionModelTiers[newSessionId] = newTier;
                delete sessionModelTiers['new'];
                fetch(`${API}/agents/${sentAgentId}/sessions/${newSessionId}/model-tier`, {
                  method: 'PUT',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ model_tier: newTier }),
                });
              }
              // 新規セッションのID: ユーザーがまだこのセッションを見ている場合のみ更新
              if (isViewingThisSession()) {
                currentSessionId = newSessionId;
              }
            }
          } catch (e) { /* 不正なJSON行は無視 */ }
        }
      }
      // スクロールは表示中のセッションのみ
      if (isViewingThisSession()) {
        document.getElementById('chat-messages').scrollTop = document.getElementById('chat-messages').scrollHeight;
      }
    }

    // エラーの場合：メッセージ表示 + セッション履歴を再取得して正しい状態に復元
    if (streamError) {
      if (thinkingEl.parentNode) thinkingEl.remove();
      if (isViewingThisSession()) {
        appendMessage('assistant', `エラー: ${streamError}`);
      }
      delete sessionStates[sentSessionId || 'new'];
      if (newSessionId) delete sessionStates['new'];
      const resolvedSid = newSessionId || sentSessionId;
      if (resolvedSid) {
        await loadSessions();
        if (currentAgentId === sentAgentId && currentSessionId === resolvedSid) {
          await loadSessionHistory(resolvedSid);
        }
      }
      return;
    }

    // 完了 — ストリーミング中はtextContentだったので、完了時にMarkdownレンダリング
    if (bubbleEl && fullResponse && typeof marked !== 'undefined') {
      bubbleEl.querySelector('.message-bubble').innerHTML = marked.parse(fullResponse);
    }

    delete sessionStates[sentSessionId || 'new'];
    if (newSessionId) delete sessionStates['new'];
    await loadSessions();

    // ユーザーが別セッションを見ていた場合、考え中表示がまだDOMに残っている可能性
    if (thinkingEl.parentNode) {
      thinkingEl.remove();
    }

  } catch (e) {
    if (thinkingEl.parentNode) {
      thinkingEl.remove();
    }
    delete sessionStates[sentSessionId || 'new'];
    if (newSessionId) delete sessionStates['new'];

    // ストリームが途切れた場合、セッション履歴を再読み込みして結果を表示する
    const resolvedSessionId = newSessionId || sentSessionId;
    if (resolvedSessionId) {
      await loadSessions();
      // 表示中のセッションなら履歴を再取得して最新状態を反映
      if (currentAgentId === sentAgentId && currentSessionId === resolvedSessionId) {
        await loadSessionHistory(resolvedSessionId);
      }
    } else if (isViewingThisSession()) {
      appendMessage('assistant', `エラー: ${e.message}`);
    }
  }
}

function appendMessage(role, content) {
  const container = document.getElementById('chat-messages');
  const agent = agents.find(a => a.id === currentAgentId);
  const sender = role === 'user' ? 'あなた' : (agent ? agent.name : '');
  const time = new Date().toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });

  const div = document.createElement('div');
  div.className = `message ${role}`;
  div.innerHTML = `
    <div class="message-sender">${escapeHtml(sender)}</div>
    <div class="message-bubble">${escapeHtml(content)}</div>
    <div class="message-time">${time}</div>
  `;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

function appendThinking() {
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'thinking-indicator';
  div.innerHTML = '<div class="spinner"></div> 応答待ち';
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

function appendToolUse(description) {
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'tool-use-notice';
  div.innerHTML = `<span class="icon">&#9881;</span> ${escapeHtml(description)}`;
  container.appendChild(div);
}

// ============================================================
// タブ切り替え
// ============================================================

function initTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });
}

function switchTab(tabName) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelector(`.tab[data-tab="${tabName}"]`).classList.add('active');

  const chatContent = document.getElementById('chat-tab-content');
  const settingsContent = document.getElementById('settings-tab-content');
  const chatPane = document.getElementById('chat-pane');
  const settingsPane = document.getElementById('settings-pane');

  if (tabName === 'chat') {
    chatContent.style.display = '';
    settingsContent.classList.remove('visible');
    chatPane.classList.remove('hidden');
    chatPane.style.display = '';
    settingsPane.classList.remove('visible');
    settingsPane.style.display = '';
  } else {
    chatContent.style.display = 'none';
    settingsContent.classList.add('visible');
    chatPane.classList.add('hidden');
    chatPane.style.display = 'none';
    settingsPane.classList.add('visible');
    settingsPane.style.display = 'flex';
    loadSettingsData();
  }
}

// ============================================================
// 設定
// ============================================================

async function loadSettingsData() {
  if (!currentAgentId) return;

  const resp = await fetch(`${API}/agents/${currentAgentId}`);
  const agent = await resp.json();

  document.querySelector('[data-field="name"]').value = agent.name || '';
  document.querySelector('[data-field="description"]').value = agent.description || '';
  document.querySelector('[data-field="model_tier"]').value = agent.model_tier || 'deep';
  document.querySelector('[data-field="path"]').value = agent.path || '';
  document.getElementById('settings-pane-header').textContent = `${agent.name} — AI設定 (CLAUDE.md)`;

  const promptResp = await fetch(`${API}/agents/${currentAgentId}/system-prompt`);
  const promptData = await promptResp.json();
  document.querySelector('[data-field="system-prompt"]').value = promptData.content || '';
}

function isMobile() {
  return window.innerWidth <= 768;
}

function initActions() {
  // モバイル: 戻るボタン
  document.getElementById('mobile-back-btn').addEventListener('click', () => {
    document.querySelector('.layout').classList.remove('mobile-chat-active');
  });
  document.getElementById('settings-back-btn').addEventListener('click', () => {
    switchTab('chat');
  });

  // 設定保存（中央ペイン）
  document.getElementById('settings-save-btn').addEventListener('click', async () => {
    await fetch(`${API}/agents/${currentAgentId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: document.querySelector('[data-field="name"]').value,
        description: document.querySelector('[data-field="description"]').value,
        model_tier: document.querySelector('[data-field="model_tier"]').value,
      }),
    });
    await loadAgents();
  });

  // CLAUDE.md保存
  document.getElementById('btn-save-prompt').addEventListener('click', async () => {
    await fetch(`${API}/agents/${currentAgentId}/system-prompt`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content: document.querySelector('[data-field="system-prompt"]').value,
      }),
    });
  });

  // CLAUDE.mdリセット
  document.getElementById('btn-reset-prompt').addEventListener('click', () => {
    loadSettingsData();
  });

  // CLI起動
  document.getElementById('btn-cli').addEventListener('click', async () => {
    if (!currentAgentId) return;
    await fetch(`${API}/agents/${currentAgentId}/cli`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: currentSessionId }),
    });
  });

  // 非表示
  document.getElementById('btn-hide').addEventListener('click', async () => {
    if (!currentAgentId || !currentSessionId) return;
    if (!confirm('この会話セッションをリストから非表示にしますか？')) return;
    await fetch(`${API}/agents/${currentAgentId}/sessions/${currentSessionId}/hide`, {
      method: 'POST',
    });
    currentSessionId = null;
    clearChat();
    await loadSessions();
  });

  // 新規会話
  document.getElementById('new-chat-btn').addEventListener('click', () => {
    currentSessionId = null;
    clearChat();
    document.querySelectorAll('.conversation-item').forEach(el => el.classList.remove('active'));
  });

  // タイトル編集
  const chatTitleEl = document.getElementById('chat-title');
  chatTitleEl.addEventListener('click', () => {
    if (!currentAgentId || !currentSessionId) return;
    const newTitle = prompt('会話タイトルを入力', currentSessionTitle);
    if (newTitle === null) return; // キャンセル
    saveSessionTitle(newTitle);
  });
}

// ============================================================
// リサイズ
// ============================================================

function initResize() {
  const handle = document.getElementById('resize-handle');
  const middlePane = document.getElementById('middle-pane');
  const STORAGE_KEY = 'kobito_agents_middle_width';
  const MIN_WIDTH = 200;
  const MAX_WIDTH = 600;

  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) {
    const w = parseInt(saved, 10);
    if (w >= MIN_WIDTH && w <= MAX_WIDTH) {
      middlePane.style.width = w + 'px';
    }
  }

  let dragging = false;
  let startX = 0;
  let startWidth = 0;

  handle.addEventListener('mousedown', (e) => {
    dragging = true;
    startX = e.clientX;
    startWidth = middlePane.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const delta = e.clientX - startX;
    let newWidth = startWidth + delta;
    newWidth = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, newWidth));
    middlePane.style.width = newWidth + 'px';
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    localStorage.setItem(STORAGE_KEY, middlePane.offsetWidth);
  });
}

// ============================================================
// ユーティリティ
// ============================================================

function showToast(message, duration = 4000) {
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = message;
  document.body.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('toast-visible'));
  setTimeout(() => {
    toast.classList.remove('toast-visible');
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
