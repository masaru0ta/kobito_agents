// kobito_agents フロントエンド

const API = '/api';
let currentAgentId = null;
let currentSessionId = null;
let agents = [];
let sessionStates = {}; // { sessionId: 'idle' | 'waiting' | 'streaming' }

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
}

// ============================================================
// セッション一覧
// ============================================================

async function loadSessions() {
  if (!currentAgentId) return;
  const resp = await fetch(`${API}/agents/${currentAgentId}/sessions`);
  const sessions = await resp.json();
  renderSessions(sessions);
}

function renderSessions(sessions) {
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
    const state = sessionStates[s.session_id];
    const statusHtml = state === 'waiting' || state === 'streaming'
      ? '<div class="conv-status"><div class="spinner"></div><span class="label">応答待ち</span></div>'
      : '';
    return `
      <div class="conversation-item${s.session_id === currentSessionId ? ' active' : ''}" data-session-id="${s.session_id}">
        <div class="conv-header">
          <span class="conv-date">${date}</span>
          <span class="conv-count">(${s.message_count})</span>
        </div>
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
  // アクティブ状態を更新
  document.querySelectorAll('.conversation-item').forEach(el => {
    el.classList.toggle('active', el.dataset.sessionId === sessionId);
  });
  await loadSessionHistory(sessionId);
}

// ============================================================
// チャット履歴
// ============================================================

async function loadSessionHistory(sessionId) {
  if (!currentAgentId || !sessionId) return;
  const resp = await fetch(`${API}/agents/${currentAgentId}/sessions/${sessionId}`);
  const messages = await resp.json();
  renderMessages(messages);

  const date = messages.length > 0
    ? new Date(messages[0].timestamp).toLocaleString('ja-JP')
    : '';
  document.getElementById('chat-title').textContent = date ? `${date} の会話` : '';
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

function clearChat() {
  document.getElementById('chat-messages').innerHTML = '';
  document.getElementById('chat-title').textContent = '';
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
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
}

async function sendMessage() {
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message || !currentAgentId) return;

  input.value = '';

  // ユーザーメッセージを即座に表示
  appendMessage('user', message);

  // 考え中表示
  const thinkingEl = appendThinking();

  // セッション状態を更新
  const sessionId = currentSessionId;
  sessionStates[sessionId || 'new'] = 'waiting';
  await loadSessions();

  try {
    const body = { message, session_id: currentSessionId };
    const resp = await fetch(`${API}/agents/${currentAgentId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    // SSEストリーミング
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let fullResponse = '';
    let newSessionId = null;

    sessionStates[sessionId || 'new'] = 'streaming';

    // 考え中を消してアシスタントバブルを追加
    thinkingEl.remove();
    const bubbleEl = appendMessage('assistant', '');

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
              fullResponse += event.data;
              bubbleEl.querySelector('.message-bubble').textContent = fullResponse;
            } else if (event.type === 'tool_use') {
              appendToolUse(event.data);
            } else if (event.type === 'session_id') {
              newSessionId = event.data;
              currentSessionId = newSessionId;
            }
          } catch (e) { /* 不正なJSON行は無視 */ }
        }
      }
      document.getElementById('chat-messages').scrollTop = document.getElementById('chat-messages').scrollHeight;
    }

    // 完了
    delete sessionStates[sessionId || 'new'];
    if (newSessionId) delete sessionStates['new'];
    await loadSessions();

  } catch (e) {
    thinkingEl.remove();
    delete sessionStates[sessionId || 'new'];
    appendMessage('assistant', `エラー: ${e.message}`);
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
  div.innerHTML = '<div class="spinner"></div> 考え中...';
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

function initActions() {
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
    await fetch(`${API}/agents/${currentAgentId}/sessions/${currentSessionId}/hide`, {
      method: 'POST',
    });
    currentSessionId = null;
    clearChat();
    await loadSessions();
  });

  // 要約
  document.getElementById('btn-summarize').addEventListener('click', async () => {
    if (!currentAgentId || !currentSessionId) return;
    await fetch(`${API}/agents/${currentAgentId}/sessions/${currentSessionId}/summarize`, {
      method: 'POST',
    });
  });

  // 新規会話
  document.getElementById('new-chat-btn').addEventListener('click', () => {
    currentSessionId = null;
    clearChat();
    document.querySelectorAll('.conversation-item').forEach(el => el.classList.remove('active'));
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

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
