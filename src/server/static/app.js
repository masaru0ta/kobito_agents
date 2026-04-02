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
  initTasks();
  initResponsive();
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
  const tasksContent = document.getElementById('tasks-tab-content');
  const chatPane = document.getElementById('chat-pane');
  const settingsPane = document.getElementById('settings-pane');
  const taskDetailPane = document.getElementById('task-detail-pane');

  // 中央ペインを切り替え
  chatContent.style.display = 'none';
  settingsContent.classList.remove('visible');
  tasksContent.style.display = 'none';

  // 右ペインを切り替え
  chatPane.classList.add('hidden');
  settingsPane.classList.remove('visible');
  taskDetailPane.classList.remove('visible');

  if (tabName === 'chat') {
    chatContent.style.display = '';
    chatPane.classList.remove('hidden');
  } else if (tabName === 'tasks') {
    tasksContent.style.display = 'flex';
    if (currentTaskId) {
      taskDetailPane.classList.add('visible');
    }
  } else if (tabName === 'settings') {
    settingsContent.classList.add('visible');
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
  return window.innerWidth <= 600;
}

function initResponsive() {
  window.addEventListener('resize', () => {
    if (!isMobile()) {
      // デスクトップ幅に広がったらモバイル専用クラスを外す
      document.querySelector('.layout').classList.remove('mobile-chat-active');
    }
  });
}

function initActions() {
  // モバイル: 戻るボタン
  document.getElementById('sidebar-toggle').addEventListener('click', () => {
    document.querySelector('.sidebar').classList.toggle('open');
  });
  // サイドバー外クリックで閉じる
  document.querySelector('.layout').addEventListener('click', e => {
    const sidebar = document.querySelector('.sidebar');
    if (sidebar.classList.contains('open') && !sidebar.contains(e.target) && e.target.id !== 'sidebar-toggle') {
      sidebar.classList.remove('open');
    }
  });

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

// ============================================================
// タスク管理（モック実装）
// ============================================================

let currentTaskId = null;
let showingDoneHistory = false;
let taskContextMenuId = null;
let pendingRejectTaskId = null;

// モックデータ
const MOCK_TASKS = {
  task_001: {
    id: 'task_001', title: 'CLAUDE.mdの編集UIの改善',
    phase: 'doing', approval: 'approved', type: 'once',
    created: '2026-04-01T15:00:00Z', agent: 'system',
    background: 'CLAUDE.mdの編集時にシンタックスハイライトがなく、長文の編集が困難。',
    policy: ['CodeMirrorを導入してMarkdownのシンタックスハイライトを実現する', 'プレビューペインを追加して編集中にリアルタイムプレビューを表示する'],
    criteria: ['Markdown記法がハイライト表示される', 'リアルタイムプレビューが表示される'],
    log: [{date: '2026/04/01 15:30', text: 'CodeMirror 6の導入を完了。基本的なMarkdownハイライトが動作している。'}, {date: '2026/04/01 16:00', text: 'プレビューペインを実装中。スタイル調整が残っている。'}],
  },
  task_002: {
    id: 'task_002', title: 'セッション一覧の読み込み速度改善',
    phase: 'draft', approval: 'approved', type: 'once',
    created: '2026-04-01T17:00:00Z', agent: 'system',
    background: 'セッション数が100を超えるとGET /api/agents/{id}/sessionsの応答が2秒以上かかる。JONLファイルを毎回全件パースしているのが原因。',
    policy: ['JONLファイルのメタ情報をキャッシュファイルに保存する', '一覧取得時はキャッシュから返し、ファイル更新日時が変わったものだけ再パースする'],
    criteria: ['セッション500件で一覧取得の応答が200ms以下', 'キャッシュ破損時にフルリビルドできる'],
    log: [],
  },
  task_003: {
    id: 'task_003', title: 'テストカバレッジの拡充',
    phase: 'draft', approval: 'approved', type: 'once',
    created: '2026-04-01T16:00:00Z', agent: 'system',
    background: '主要コンポーネントのテストカバレッジが低い状態。',
    policy: ['各コンポーネントのユニットテストを追加する', 'integration testを追加する'],
    criteria: ['カバレッジ80%以上'],
    log: [],
  },
  task_004: {
    id: 'task_004', title: 'エージェント登録APIの追加',
    phase: 'draft', approval: 'pending', type: 'once',
    created: '2026-04-01T16:30:00Z', agent: 'system',
    background: '現在エージェントはagents.jsonを直接編集して追加するしかない。Web UIから追加できるようにする。',
    policy: ['POST /api/agents エンドポイントを追加する', 'UIにエージェント追加フォームを追加する'],
    criteria: ['UIからエージェントを登録できる'],
    log: [],
  },
  task_005: {
    id: 'task_005', title: 'Codex対応の調査',
    phase: 'draft', approval: 'pending', type: 'once',
    created: '2026-04-01T18:00:00Z', agent: 'system',
    background: 'Phase 1ではClaude Codeのみ対応。Codexへの対応が未実装。',
    policy: ['Codex APIの仕様を調査する', 'CodexSessionReaderの設計案を作成する'],
    criteria: ['調査レポートを作成する'],
    log: [],
  },
  task_006: {
    id: 'task_006', title: 'ConfigManagerの実装',
    phase: 'done', approval: 'approved', type: 'once',
    created: '2026-03-31T10:00:00Z', agent: 'system',
    background: '', policy: [], criteria: [], log: [],
  },
  task_007: {
    id: 'task_007', title: 'SessionReaderの実装',
    phase: 'done', approval: 'approved', type: 'once',
    created: '2026-03-30T16:00:00Z', agent: 'system',
    background: '', policy: [], criteria: [], log: [],
  },
  task_008: {
    id: 'task_008', title: 'プロジェクト初期構成',
    phase: 'done', approval: 'approved', type: 'once',
    created: '2026-03-29T11:00:00Z', agent: 'system',
    background: '', policy: [], criteria: [], log: [],
  },
  task_sched_001: {
    id: 'task_sched_001', title: '日次レポート生成',
    phase: 'done', approval: 'approved', type: 'scheduled',
    schedule: '毎日 09:00', schedActive: true, nextRun: '04/02 09:00',
    created: '2026-04-01T09:00:00Z', agent: 'system',
    background: '毎日の作業ログとセッション統計を集計してレポートを生成する。',
    policy: ['セッション一覧から当日のデータを集計する', 'Markdownレポートとして保存する'],
    criteria: ['毎日09:00に自動生成される'],
    log: [{date: '2026/04/01 09:05', text: 'レポートを生成しました。本日のセッション数: 3件。'}],
  },
  task_sched_002: {
    id: 'task_sched_002', title: '週次コードレビュー',
    phase: 'draft', approval: 'approved', type: 'scheduled',
    schedule: '毎週 月曜 10:00', schedActive: true, nextRun: null,
    created: '2026-04-01T10:00:00Z', agent: 'system',
    background: '週に一度、先週のコード変更を振り返りレビューする。',
    policy: ['git logから直近1週間の変更を取得する', '変更内容を分析してレポートを作成する'],
    criteria: ['レビューレポートが生成される'],
    log: [],
  },
  task_sched_003: {
    id: 'task_sched_003', title: '月次依存パッケージ更新確認',
    phase: 'done', approval: 'approved', type: 'scheduled',
    schedule: '毎月 1日 09:00', schedActive: false, nextRun: '05/01 09:00',
    created: '2026-04-01T09:00:00Z', agent: 'system',
    background: 'パッケージの脆弱性・更新情報を月次で確認する。',
    policy: ['pip listでインストール済みパッケージを確認する', '更新が必要なものを報告する'],
    criteria: ['更新確認レポートが生成される'],
    log: [],
  },
};

// 実行キューの順序（承認済みonce typeのタスクID順）
let taskExecutionOrder = ['task_001', 'task_002', 'task_003'];

function initTasks() {
  renderTaskList();
  updatePendingBadge();

  // コンテキストメニューの外クリックで閉じる
  document.addEventListener('click', (e) => {
    const menu = document.getElementById('task-context-menu');
    if (!menu.contains(e.target) && !e.target.classList.contains('task-more-btn')) {
      menu.classList.remove('visible');
    }
  });

  // コンテキストメニュー項目
  document.getElementById('ctx-edit').addEventListener('click', () => {
    document.getElementById('task-context-menu').classList.remove('visible');
    enterTaskEditMode();
  });

  document.getElementById('ctx-force-done').addEventListener('click', () => {
    document.getElementById('task-context-menu').classList.remove('visible');
    if (!taskContextMenuId) return;
    const task = MOCK_TASKS[taskContextMenuId];
    if (task) { task.phase = 'done'; renderTaskList(); renderTaskDetail(taskContextMenuId); }
  });

  document.getElementById('ctx-delete').addEventListener('click', () => {
    document.getElementById('task-context-menu').classList.remove('visible');
    if (!taskContextMenuId) return;
    if (!confirm(`「${MOCK_TASKS[taskContextMenuId]?.title}」を削除しますか？`)) return;
    delete MOCK_TASKS[taskContextMenuId];
    taskExecutionOrder = taskExecutionOrder.filter(id => id !== taskContextMenuId);
    currentTaskId = null;
    renderTaskList();
    updatePendingBadge();
    document.getElementById('task-detail-pane').classList.remove('visible');
  });

  // 却下ダイアログ
  document.getElementById('reject-cancel').addEventListener('click', hideRejectDialog);
  document.getElementById('reject-confirm').addEventListener('click', () => {
    if (!pendingRejectTaskId) return;
    const task = MOCK_TASKS[pendingRejectTaskId];
    if (task) { task.approval = 'rejected'; }
    hideRejectDialog();
    renderTaskList();
    renderTaskDetail(pendingRejectTaskId);
    updatePendingBadge();
  });
}

function updatePendingBadge() {
  const count = Object.values(MOCK_TASKS).filter(t => t.approval === 'pending').length;
  const badge = document.getElementById('pending-badge');
  if (count > 0) {
    badge.textContent = count;
    badge.style.display = '';
  } else {
    badge.style.display = 'none';
  }
}

function renderTaskList() {
  const list = document.getElementById('task-list');
  let html = '';

  // 実行キュー（承認済みonce type、taskExecutionOrderに従う）
  taskExecutionOrder.forEach((id, i) => {
    const task = MOCK_TASKS[id];
    if (!task || task.type !== 'once' || task.approval !== 'approved') return;
    const isFirst = i === 0;
    const active = id === currentTaskId ? ' active' : '';
    const indicator = isFirst
      ? '<span class="doing-indicator"><span class="dot"></span>実行中</span>'
      : '';
    html += `
      <div class="task-queue-row" data-row-id="${id}">
        <span class="task-order-num">${i + 1}</span>
        <div class="task-item draggable${active}" data-task-id="${id}" draggable="true">
          <div class="task-item-header">
            <span class="drag-handle">&#x2630;</span>
            <div class="task-title-group">
              <span class="task-item-title">${escapeHtml(task.title)}</span>
              ${indicator}
            </div>
          </div>
          <div class="task-item-meta">
            <span>${formatTaskDate(task.created)}</span>
          </div>
        </div>
      </div>`;
  });

  // 承認待ち
  const pendingTasks = Object.values(MOCK_TASKS).filter(t => t.type === 'once' && t.approval === 'pending');
  pendingTasks.forEach(task => {
    const active = task.id === currentTaskId ? ' active' : '';
    html += `
      <div class="task-item task-item-indented${active}" data-task-id="${task.id}">
        <div class="task-item-header">
          <div class="task-title-group">
            <span class="task-item-title">${escapeHtml(task.title)}</span>
            <span class="badge badge-pending">承認待ち</span>
          </div>
        </div>
        <div class="task-item-meta">
          <span>${formatTaskDate(task.created)}</span>
        </div>
      </div>`;
  });

  // 却下済み（一応表示）
  const rejectedTasks = Object.values(MOCK_TASKS).filter(t => t.type === 'once' && t.approval === 'rejected');
  rejectedTasks.forEach(task => {
    const active = task.id === currentTaskId ? ' active' : '';
    html += `
      <div class="task-item task-item-indented${active}" data-task-id="${task.id}" style="opacity:0.4;">
        <div class="task-item-header">
          <div class="task-title-group">
            <span class="task-item-title">${escapeHtml(task.title)}</span>
            <span class="badge" style="background:#f8514933;color:var(--danger);border:1px solid var(--danger);">却下</span>
          </div>
        </div>
        <div class="task-item-meta"><span>${formatTaskDate(task.created)}</span></div>
      </div>`;
  });

  // 完了タスク（最新1件 + 履歴）
  const doneTasks = Object.values(MOCK_TASKS)
    .filter(t => t.type === 'once' && t.phase === 'done' && t.approval === 'approved')
    .sort((a, b) => b.created.localeCompare(a.created));

  if (doneTasks.length > 0) {
    const latest = doneTasks[0];
    const active = latest.id === currentTaskId ? ' active' : '';
    html += `
      <div class="task-item task-item-indented task-last-done${active}" data-task-id="${latest.id}">
        <div class="task-item-header">
          <div class="task-title-group">
            <span class="task-item-title">${escapeHtml(latest.title)}</span>
            <span class="badge badge-done">done</span>
          </div>
        </div>
        <div class="task-item-meta"><span>${formatTaskDate(latest.created)} 完了</span></div>
      </div>`;

    if (doneTasks.length > 1) {
      const histItems = doneTasks.slice(1).map(task => {
        const a = task.id === currentTaskId ? ' active' : '';
        return `
          <div class="task-item task-item-indented${a}" data-task-id="${task.id}">
            <div class="task-item-header">
              <div class="task-title-group">
                <span class="task-item-title">${escapeHtml(task.title)}</span>
                <span class="badge badge-done">done</span>
              </div>
            </div>
            <div class="task-item-meta"><span>${formatTaskDate(task.created)} 完了</span></div>
          </div>`;
      }).join('');

      html += `
        <div class="task-done-history" id="task-done-history" style="${showingDoneHistory ? '' : 'display:none;'}">
          ${histItems}
        </div>
        <div class="task-done-toggle" id="task-done-toggle">
          ${showingDoneHistory ? '過去の完了タスクを隠す' : `過去の完了タスクを表示 (${doneTasks.length - 1})`}
        </div>`;
    }
  }

  // HR
  html += '<hr class="task-list-divider">';

  // スケジュールタスク
  const scheduledTasks = Object.values(MOCK_TASKS).filter(t => t.type === 'scheduled');
  scheduledTasks.forEach(task => {
    const active = task.id === currentTaskId ? ' active' : '';
    const isDone = task.phase === 'done';
    const schedBadge = task.schedActive
      ? `<span class="badge badge-sched-active" data-sched-id="${task.id}">定期実行</span>`
      : `<span class="badge badge-sched-stopped" data-sched-id="${task.id}">停止中</span>`;
    const nextRunHtml = task.nextRun
      ? `&nbsp;›&nbsp;<span class="next-run">次回 ${task.nextRun}</span>`
      : '';
    html += `
      <div class="task-item task-item-indented${isDone ? ' task-sched-done' : ''}${active}" data-task-id="${task.id}">
        <div class="task-item-header">
          <div class="task-title-group">
            <span class="task-item-title">${escapeHtml(task.title)}</span>
            ${isDone ? '<span class="badge badge-done">done</span>' : ''}
            ${schedBadge}
          </div>
        </div>
        <div class="task-item-meta">
          <span class="task-schedule-info">${task.schedule}${nextRunHtml}</span>
        </div>
      </div>`;
  });

  list.innerHTML = html;

  // クリックイベント
  list.querySelectorAll('.task-item[data-task-id]').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('.drag-handle') || e.target.closest('[data-sched-id]')) return;
      selectTask(el.dataset.taskId);
    });
  });

  // スケジュールトグル
  list.querySelectorAll('[data-sched-id]').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      const id = el.dataset.schedId;
      const task = MOCK_TASKS[id];
      if (!task) return;
      task.schedActive = !task.schedActive;
      renderTaskList();
      if (currentTaskId === id) renderTaskDetail(id);
    });
  });

  // 完了履歴トグル
  const toggleEl = document.getElementById('task-done-toggle');
  if (toggleEl) {
    toggleEl.addEventListener('click', () => {
      showingDoneHistory = !showingDoneHistory;
      renderTaskList();
    });
  }

  // ドラッグ&ドロップ（実行キュー）
  initTaskDragDrop();
}

function selectTask(taskId) {
  currentTaskId = taskId;
  renderTaskList();

  // 右ペインをタスク詳細に切り替え
  document.getElementById('chat-pane').classList.add('hidden');
  document.getElementById('settings-pane').classList.remove('visible');
  document.getElementById('task-detail-pane').classList.add('visible');

  renderTaskDetail(taskId);
}

function renderTaskDetail(taskId) {
  const task = MOCK_TASKS[taskId];
  if (!task) return;

  // ヘッダー
  const headerEl = document.getElementById('task-detail-header');
  let approvalHtml = '';
  if (task.approval === 'pending') {
    approvalHtml = `
      <button class="approval-btn approve" data-approve="${taskId}">承認</button>
      <button class="approval-btn reject" data-reject="${taskId}">却下</button>`;
  } else if (task.approval === 'approved') {
    approvalHtml = `<div class="approval-status-badge approved">✓ 承認済み</div>`;
  } else if (task.approval === 'rejected') {
    approvalHtml = `<div class="approval-status-badge rejected">✗ 却下</div>`;
  }

  const phaseBadgeMap = { draft: '', doing: '<span class="doing-indicator"><span class="dot"></span>実行中</span>', done: '<span class="badge badge-done">done</span>' };
  const phaseBadge = phaseBadgeMap[task.phase] || '';

  headerEl.innerHTML = `
    <div class="task-detail-header-left">
      ${phaseBadge}
      <span class="task-detail-title">${escapeHtml(task.title)}</span>
    </div>
    <div class="task-detail-actions">
      ${approvalHtml}
      <button class="task-more-btn" data-more="${taskId}" title="その他">&#x22EF;</button>
    </div>`;

  // 承認/却下ボタンのイベント
  headerEl.querySelector(`[data-approve="${taskId}"]`)?.addEventListener('click', () => {
    task.approval = 'approved';
    taskExecutionOrder.push(taskId);
    renderTaskList();
    renderTaskDetail(taskId);
    updatePendingBadge();
  });

  headerEl.querySelector(`[data-reject="${taskId}"]`)?.addEventListener('click', () => {
    pendingRejectTaskId = taskId;
    showRejectDialog();
  });

  // ... メニューボタン
  headerEl.querySelector(`[data-more="${taskId}"]`)?.addEventListener('click', (e) => {
    e.stopPropagation();
    taskContextMenuId = taskId;
    const menu = document.getElementById('task-context-menu');
    const rect = e.currentTarget.getBoundingClientRect();
    menu.style.top = (rect.bottom + 4) + 'px';
    menu.style.right = (window.innerWidth - rect.right) + 'px';
    menu.style.left = '';
    menu.classList.toggle('visible');
    // 完了済みなら強制完了を無効化
    document.getElementById('ctx-force-done').style.opacity = task.phase === 'done' ? '0.4' : '';
    document.getElementById('ctx-force-done').style.pointerEvents = task.phase === 'done' ? 'none' : '';
  });

  // ボディ
  const bodyEl = document.getElementById('task-detail-body');
  const created = new Date(task.created).toLocaleString('ja-JP', { year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' });

  let contentHtml = '';
  if (task.background) {
    contentHtml += `<h2>背景</h2><p>${escapeHtml(task.background)}</p>`;
  }
  if (task.policy && task.policy.length > 0) {
    contentHtml += `<h2>方針</h2><ol>${task.policy.map(p => `<li>${escapeHtml(p)}</li>`).join('')}</ol>`;
  }
  if (task.criteria && task.criteria.length > 0) {
    contentHtml += `<h2>完了条件</h2><ul>${task.criteria.map(c => `<li>${escapeHtml(c)}</li>`).join('')}</ul>`;
  }
  if (task.log && task.log.length > 0) {
    contentHtml += `<h2>作業ログ</h2>${task.log.map(l => `<div class="log-entry"><div class="log-date">${escapeHtml(l.date)}</div>${escapeHtml(l.text)}</div>`).join('')}`;
  }

  bodyEl.innerHTML = `
    <div class="task-meta-bar">
      <div class="task-meta-item"><span class="task-meta-label">ID:</span><span>${escapeHtml(task.id)}</span></div>
      <div class="task-meta-item"><span class="task-meta-label">担当:</span><span>${escapeHtml(task.agent)}</span></div>
      <div class="task-meta-item"><span class="task-meta-label">起票:</span><span>${created}</span></div>
      ${task.type === 'scheduled' ? `<div class="task-meta-item"><span class="task-meta-label">スケジュール:</span><span>${escapeHtml(task.schedule)}</span></div>` : ''}
    </div>
    <div class="task-md-content">${contentHtml}</div>`;
}

function enterTaskEditMode() {
  const task = MOCK_TASKS[currentTaskId];
  if (!task) return;

  // 本文を textarea に変換
  const bodyEl = document.getElementById('task-detail-body');
  const content = [
    task.background ? `## 背景\n${task.background}` : '',
    task.policy?.length ? `## 方針\n${task.policy.map((p, i) => `${i+1}. ${p}`).join('\n')}` : '',
    task.criteria?.length ? `## 完了条件\n${task.criteria.map(c => `- ${c}`).join('\n')}` : '',
  ].filter(Boolean).join('\n\n');

  // meta-bar は維持しつつ md-content を textarea に置き換え
  const mdContent = bodyEl.querySelector('.task-md-content');
  if (mdContent) {
    mdContent.innerHTML = `
      <textarea class="task-edit-textarea" id="task-edit-textarea">${escapeHtml(content)}</textarea>
      <div class="task-edit-actions">
        <button class="chat-action-btn" id="task-edit-cancel">キャンセル</button>
        <button class="settings-save-btn" id="task-edit-save">保存</button>
      </div>`;

    document.getElementById('task-edit-cancel').addEventListener('click', () => {
      renderTaskDetail(currentTaskId);
    });

    document.getElementById('task-edit-save').addEventListener('click', () => {
      // 簡易パース: 背景/方針/完了条件を抽出して保存
      const text = document.getElementById('task-edit-textarea').value;
      task.background = extractSection(text, '背景') || task.background;
      const policyText = extractSection(text, '方針');
      if (policyText) task.policy = policyText.split('\n').map(l => l.replace(/^\d+\.\s*/, '').trim()).filter(Boolean);
      const criteriaText = extractSection(text, '完了条件');
      if (criteriaText) task.criteria = criteriaText.split('\n').map(l => l.replace(/^[-*]\s*/, '').trim()).filter(Boolean);
      renderTaskDetail(currentTaskId);
    });
  }
}

function extractSection(text, heading) {
  const re = new RegExp(`##\\s+${heading}\\s*\\n([\\s\\S]*?)(?=\\n##\\s|$)`);
  const m = text.match(re);
  return m ? m[1].trim() : null;
}

function formatTaskDate(iso) {
  const d = new Date(iso);
  return d.toLocaleString('ja-JP', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' }).replace(/\//g, '/');
}

function showRejectDialog() {
  document.getElementById('reject-reason').value = '';
  document.getElementById('task-reject-dialog').classList.add('visible');
}

function hideRejectDialog() {
  document.getElementById('task-reject-dialog').classList.remove('visible');
  pendingRejectTaskId = null;
}

function initTaskDragDrop() {
  let dragRowId = null;

  document.querySelectorAll('.task-queue-row').forEach(row => {
    const item = row.querySelector('.task-item.draggable');
    if (!item) return;

    item.addEventListener('dragstart', (e) => {
      dragRowId = row.dataset.rowId;
      row.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    });

    item.addEventListener('dragend', () => {
      row.classList.remove('dragging');
      document.querySelectorAll('.task-queue-row.drag-over').forEach(el => el.classList.remove('drag-over'));
      dragRowId = null;
    });

    row.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (row.dataset.rowId === dragRowId) return;
      e.dataTransfer.dropEffect = 'move';
      row.classList.add('drag-over');
    });

    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));

    row.addEventListener('drop', (e) => {
      e.preventDefault();
      row.classList.remove('drag-over');
      if (!dragRowId || row.dataset.rowId === dragRowId) return;
      const fromIdx = taskExecutionOrder.indexOf(dragRowId);
      const toIdx = taskExecutionOrder.indexOf(row.dataset.rowId);
      if (fromIdx < 0 || toIdx < 0) return;
      taskExecutionOrder.splice(fromIdx, 1);
      taskExecutionOrder.splice(toIdx, 0, dragRowId);
      renderTaskList();
    });
  });
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
