// kobito_agents フロントエンド

const API = '/api';
let currentAgentId = null;
let currentSessionId = null;
let agents = [];
let sessions = [];
let inferringSessions = new Set(); // 推論中のセッションID（バックエンド権威 + 送信時の楽観更新）
let activeStreams = new Set();    // SSEストリーム中のセッションID
let sessionModelTiers = {}; // { sessionId: 'deep' | 'quick' }
let processStatusInterval = null;
let lastDirMtime = 0;     // セッションディレクトリの前回mtime
let lastWatchingMtime = 0; // 表示中セッションJSONLの前回mtime
let lastStartupId = null;  // サーバー起動IDキャッシュ（変化でリロード検知）
const sessionDomCache = {}; // { sessionId: { el: HTMLElement, stale: boolean } } セッションDOMキャッシュ
let currentStreamAbort = null; // 現在アクティブなSSEストリームのAbortController

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
  initReports();
  initResponsive();

  // サーバー再起動ボタン
  document.getElementById('restart-btn').addEventListener('click', async () => {
    if (!confirm('サーバーを再起動しますか？')) return;
    try {
      await fetch(`${API}/restart`, { method: 'POST' });
    } catch (_) {}
    showToast('サーバーを再起動しています...');
    // サーバー復帰を待ってリロード
    const wait = () => new Promise(r => setTimeout(r, 2000));
    for (let i = 0; i < 15; i++) {
      await wait();
      try {
        const resp = await fetch(`${API}/agents`);
        if (resp.ok) { location.reload(); return; }
      } catch (_) {}
    }
    showToast('再起動がタイムアウトしました');
  });

  // スケジューラートグルボタン
  initScheduler();
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
  // タスク詳細を閉じてタスク一覧を読み込む
  backToTaskList();
  await loadTasks();
  // レポート詳細を閉じてレポート一覧を読み込む
  document.getElementById('report-detail-pane').style.display = 'none';
  document.getElementById('report-list-view').style.display = 'flex';
  loadReports();
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
        console.warn('[RESTART] startup_id変化 (ポーリング検知):', lastStartupId, '->', data.startup_id, new Date().toISOString());
        showToast(`サーバーが再起動されました [${data.startup_id.slice(0, 8)}]`);
        await loadSessions();
        if (currentSessionId) await loadSessionHistory(currentSessionId);
      }
      lastStartupId = data.startup_id;
    }

    // バックエンドの推論中判定を権威とし、インジケータを同期する
    const prev = inferringSessions;
    inferringSessions = new Set(data.inferring);
    syncInferringIndicators(prev, inferringSessions);

    // プロセスデバッグ情報を表示
    renderProcessDebug(data.processes || []);

    // セッションディレクトリ変化 → 一覧を再取得 + キャッシュ無効化
    if (data.dir_mtime && data.dir_mtime !== lastDirMtime) {
      if (lastDirMtime !== 0) {
        Object.entries(sessionDomCache).forEach(([key, cache]) => {
          if (key !== currentSessionId) cache.stale = true;
        });
        await loadSessions();
      }
      lastDirMtime = data.dir_mtime;
    }

    // 表示中セッション変化 → 履歴を強制再取得
    // SSEストリーム中のみスキップ（推論中でもSSEがなければ更新する）
    if (data.watching_mtime && data.watching_mtime !== lastWatchingMtime) {
      if (lastWatchingMtime !== 0 && !activeStreams.has(currentSessionId)) {
        await loadSessionHistory(currentSessionId, true);
        await loadSessions();
      }
      lastWatchingMtime = data.watching_mtime;
    }
  } catch (_) {}
}

function renderProcessDebug(processes) {
  const el = document.getElementById('process-debug');
  if (!processes.length) { el.innerHTML = ''; return; }
  const items = processes.map(p => {
    const s = sessions.find(s => s.session_id === p.session_id);
    const title = s?.title || s?.last_message?.slice(0, 20) || p.session_id.slice(0, 8);
    const srcTag = p.source === 'pidfile' ? ' [孤児]' : '';
    const tcp = p.connected ? '接続中' : 'なし';
    const tcpClass = p.connected ? 'connected' : 'alive';
    const jsonlRole = p.jsonl_last_role ?? '-';
    const jsonlClass = p.jsonl_last_role === 'assistant' ? 'alive' : p.jsonl_last_role === 'user' ? 'connected' : 'alive';
    const inferLabel = p.inferring ? '推論中' : '待機';
    const inferClass = p.inferring ? 'connected' : 'alive';
    // 安定性は推論中のときのみ表示
    const stableSpan = p.inferring
      ? (() => {
          const label = p.jsonl_stable == null ? '-'
            : p.jsonl_stable ? `安定(${p.jsonl_stable_secs ?? '?'}s)` : `変化中(${p.jsonl_stable_secs ?? '?'}s)`;
          const cls = p.jsonl_stable ? 'alive' : 'connected';
          return `<span class="process-debug-status ${cls}">${label}</span>`;
        })()
      : '';
    return `<div class="process-debug-item"><span class="process-debug-sid" title="${p.session_id}">PID:${p.pid} ${escapeHtml(title)}${srcTag}</span></div><div class="process-debug-item process-debug-sub"><span class="process-debug-status ${inferClass}">${inferLabel}</span><span class="process-debug-status ${tcpClass}">TCP:${tcp}</span><span class="process-debug-status ${jsonlClass}">JSONL:${jsonlRole}</span>${stableSpan}</div>`;
  }).join('');
  el.innerHTML = `<div class="process-debug-title">プロセス (${processes.length})</div>${items}`;
}

function syncInferringIndicators(prev, current) {
  // 推論終了したセッション → インジケータ除去
  prev.forEach(sid => {
    if (!current.has(sid)) {
      // ストリームがまだアクティブなら除去しない（バックエンドが一時的に推論中を返さない場合の誤除去防止）
      if (activeStreams.has(sid)) return;
      const container = getSessionContainer(sid);
      const ind = container.querySelector('.thinking-indicator');
      if (ind) { ind._stopTimer?.(); ind.remove(); }
      const el = document.querySelector(`.conversation-item[data-session-id="${sid}"] .conv-status`);
      if (el) el.remove();
      if (sid === currentSessionId) loadSessionHistory(sid, true);
    }
  });
  // 推論開始を検知（バックエンドで検知したがフロントに反映されていない）→ インジケータ追加
  current.forEach(sid => {
    if (!prev.has(sid)) {
      const el = document.querySelector(`.conversation-item[data-session-id="${sid}"]`);
      if (el && !el.querySelector('.conv-status')) {
        const div = document.createElement('div');
        div.className = 'conv-status';
        div.innerHTML = '<div class="spinner"></div><span class="label">応答待ち</span>';
        el.appendChild(div);
      }
      // ストリームが既に終了済みのセッションには thinkingEl を再追加しない
      // （レスポンス受信後にバックエンドがJSONL安定待ちで推論中返すことで起きるフリッカー防止）
      if (sid === currentSessionId && activeStreams.has(sid)) {
        const container = getSessionContainer(sid);
        if (!container.querySelector('.thinking-indicator')) appendThinking();
      }
    }
  });
}

function startProcessStatusPolling() {
  if (processStatusInterval) clearInterval(processStatusInterval);
  inferringSessions = new Set();
  const poll = () => { pollProcessStatus(); loadTasks(); };
  poll();
  processStatusInterval = setInterval(poll, 5000);
}

// ============================================================
// セッション一覧
// ============================================================

async function loadSessions() {
  if (!currentAgentId) return;
  const resp = await fetch(`${API}/agents/${currentAgentId}/sessions`);
  sessions = await resp.json();
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
    const isInferring = inferringSessions.has(s.session_id);
    const statusHtml = isInferring
      ? '<div class="conv-status"><div class="spinner"></div><span class="label">応答待ち</span></div>'
      : '';
    const titleHtml = s.title
      ? `<div class="conv-title">${escapeHtml(s.title)}</div>`
      : '';
    return `
      <div class="conversation-item${s.session_id === currentSessionId ? ' active' : ''}" data-session-id="${s.session_id}">
        <div class="conv-header">
          <span class="conv-date">${date}</span>
          <span class="conv-header-right">
            <span class="conv-count">(${s.message_count})</span>
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
  // キャッシュされたコンテナを即時表示（切替を高速化）
  activateSessionContainer(sessionId);
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

async function loadSessionHistory(sessionId, force = false) {
  if (!currentAgentId || !sessionId) return;
  const cache = sessionDomCache[sessionId];

  // セッション一覧からタイトルを取得（キャッシュ有効でも常に更新）
  const currentSession = sessions.find(s => s.session_id === sessionId);
  currentSessionTitle = currentSession?.title || '';

  // キャッシュが有効 (stale でない) 場合はメッセージ再取得をスキップ
  if (!force && cache && !cache.stale) {
    cache.el.scrollTop = cache.el.scrollHeight;
    document.getElementById('chat-title').textContent = currentSessionTitle || '';
    return;
  }

  const resp = await fetch(`${API}/agents/${currentAgentId}/sessions/${sessionId}`);
  const messages = await resp.json();
  renderMessages(messages, sessionId);

  const date = messages.length > 0
    ? new Date(messages[0].timestamp).toLocaleString('ja-JP')
    : '';
  document.getElementById('chat-title').textContent = currentSessionTitle || (date ? `${date} の会話` : '');
}

function renderMessages(messages, sessionId) {
  const container = getSessionContainer(sessionId || currentSessionId);
  // innerHTML クリア前に残留 thinkingEl のタイマーを停止
  container.querySelectorAll('.thinking-indicator').forEach(el => el._stopTimer?.());
  container.innerHTML = '';
  // キャッシュを有効にマーク
  const cacheKey = sessionId || currentSessionId;
  if (sessionDomCache[cacheKey]) sessionDomCache[cacheKey].stale = false;

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
  // エージェント切替・非表示等: キャッシュを全消去
  Object.values(sessionDomCache).forEach(c => c.el.remove());
  Object.keys(sessionDomCache).forEach(k => delete sessionDomCache[k]);
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

  document.getElementById('stop-btn').addEventListener('click', async () => {
    const sid = currentSessionId;
    // SSEストリームを中断
    if (currentStreamAbort) {
      currentStreamAbort.abort();
      currentStreamAbort = null;
    }
    // サーバー側のプロセスを停止
    if (sid && currentAgentId) {
      await fetch(`${API}/agents/${currentAgentId}/sessions/${sid}/stop`, { method: 'POST' });
    }
  });
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

async function openTalkSession(taskId) {
  const task = tasksCache[taskId];
  if (!task) return;

  // 既存の相談セッションがあれば開く
  if (task.talk_session_id) {
    switchTab('chat');
    await selectSession(task.talk_session_id);
    return;
  }

  // 送信前のセッション一覧を記録
  const prevSessionIds = new Set(sessions.map(s => s.session_id));

  // 新規セッションを作成して送信
  currentSessionId = null;
  clearChat();
  activateSessionContainer(null);
  document.querySelectorAll('.conversation-item').forEach(el => el.classList.remove('active'));
  switchTab('chat');

  const input = document.getElementById('chat-input');
  input.value = `タスク「${task.title}」について話したい。`;
  await sendMessage({ task_id: taskId, task_mode: 'talk' });

  // 送信後のセッション一覧から新規セッションを特定
  await loadSessions();
  const newSession = sessions.find(s => !prevSessionIds.has(s.session_id));
  if (!newSession) return;

  const sid = newSession.session_id;
  await fetch(`${API}/agents/${currentAgentId}/sessions/${sid}/title`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: `${task.title} の相談` }),
  });
  await fetch(`${API}/agents/${currentAgentId}/tasks/${taskId}/talk-session`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid }),
  });
  await loadSessions();
  await loadTasks();
  await selectSession(sid);
}

async function startWorkSession(taskId, message) {
  const task = tasksCache[taskId];
  if (!task) return;

  // 既存の作業セッションがある場合は最新のものを再利用
  if (task.sessions && task.sessions.length > 0) {
    const lastSid = task.sessions[task.sessions.length - 1];
    document.getElementById('chat-pane').classList.remove('hidden');
    switchTab('chat');
    await selectSession(lastSid);
    const input = document.getElementById('chat-input');
    input.value = message;
    await sendMessage();
    return;
  }

  // 新規セッション作成
  const prevSessionIds = new Set(sessions.map(s => s.session_id));

  currentSessionId = null;
  clearChat();
  activateSessionContainer(null);
  document.querySelectorAll('.conversation-item').forEach(el => el.classList.remove('active'));
  switchTab('chat');

  const input = document.getElementById('chat-input');
  input.value = message;
  await sendMessage();

  await loadSessions();
  const newSession = sessions.find(s => !prevSessionIds.has(s.session_id));
  if (!newSession) return;

  const sid = newSession.session_id;
  await fetch(`${API}/agents/${currentAgentId}/sessions/${sid}/title`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: `${task.title} の作業` }),
  });
  await fetch(`${API}/agents/${currentAgentId}/tasks/${taskId}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid }),
  });
  await loadTasks();
}

async function sendMessage(opts = {}) {
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message || !currentAgentId) return;

  input.value = '';

  // 送信時のセッションIDを記憶（別セッションに切り替わっても追跡できるように）
  let sentSessionId = currentSessionId;
  const sentAgentId = currentAgentId;

  // 現在のセッションを表示中かどうか判定するヘルパー
  const isViewingThisSession = () =>
    currentAgentId === sentAgentId && currentSessionId === sentSessionId;

  // 送信先セッションのコンテナを確実に表示（新規セッション含む）
  activateSessionContainer(sentSessionId);

  // ユーザーメッセージを即座に表示
  appendMessage('user', message);

  // 考え中表示
  const thinkingEl = appendThinking();

  // 楽観的にインジケータ表示
  const streamKey = sentSessionId || 'new';
  inferringSessions.add(streamKey);
  activeStreams.add(streamKey);

  // 中断ボタンを表示
  const stopBtn = document.getElementById('stop-btn');
  const sendBtn = document.getElementById('send-btn');
  stopBtn.style.display = '';
  sendBtn.style.display = 'none';

  await loadSessions();

  // AbortControllerを設定して中断可能にする
  const abort = new AbortController();
  currentStreamAbort = abort;

  try {
    const modelTier = document.getElementById('model-select').value;
    const body = { message, session_id: sentSessionId, model_tier: modelTier };
    if (opts.task_id) body.task_id = opts.task_id;
    if (opts.task_mode) body.task_mode = opts.task_mode;
    const resp = await fetch(`${API}/agents/${sentAgentId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: abort.signal,
    });

    // SSEストリーミング
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let fullResponse = '';
    let newSessionId = null;
    let streamError = null;

    // ストリーミング書き込み先コンテナ（キャッシュから取得）
    let streamContainer = getSessionContainer(sentSessionId);

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
                if (!bubbleEl) {
                  bubbleEl = appendMessage('assistant', '');
                }
                bubbleEl.querySelector('.message-bubble').textContent = fullResponse;
                // thinkingElを常に末尾に移動してスクロールアウトを防ぐ
                if (thinkingEl.parentNode) streamContainer.appendChild(thinkingEl);
              }
            } else if (event.type === 'tool_use') {
              if (isViewingThisSession()) {
                appendToolUse(event.data);
                // tool_use追加後もthinkingElを末尾に移動
                if (thinkingEl.parentNode) streamContainer.appendChild(thinkingEl);
              }
            } else if (event.type === 'error') {
              streamError = event.data;
              console.error('[STREAM] error event受信:', event.data);
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
                // DOMキャッシュのキーを 旧sentSessionId → newSessionId に移動
                const oldKey = sentSessionId || 'new';
                currentSessionId = newSessionId;
                sentSessionId = newSessionId; // isViewingThisSession()が以降もtrueを返すように同期
                // activeStreamsのキーも新IDに移行
                activeStreams.delete(oldKey);
                activeStreams.add(newSessionId);
                if (sessionDomCache[oldKey] && !sessionDomCache[newSessionId]) {
                  sessionDomCache[newSessionId] = sessionDomCache[oldKey];
                  delete sessionDomCache[oldKey];
                  streamContainer = sessionDomCache[newSessionId].el;
                }
              }
            }
          } catch (e) { /* 不正なJSON行は無視 */ }
        }
      }
      // スクロールは表示中のセッションのみ
      if (isViewingThisSession()) {
        streamContainer.scrollTop = streamContainer.scrollHeight;
      }
    }

    // エラーの場合: ストリーム追跡は解除し、インジケーターはポーリングに委ねる
    if (streamError) {
      activeStreams.delete(sentSessionId || 'new');
      if (newSessionId) activeStreams.delete('new');
      lastWatchingMtime = 0;
      if (streamError.includes('再起動')) {
        console.warn('[STREAM] 再起動検知(SSEエラー) sid:', sentSessionId, 'error:', streamError);
        showToast('サーバーが再起動されました。応答を受信待ちです...');
      }
      stopBtn.style.display = 'none';
      sendBtn.style.display = '';
      currentStreamAbort = null;
      await loadSessions();
      return;
    }

    // 完了 — Markdownレンダリング
    if (bubbleEl && fullResponse && typeof marked !== 'undefined') {
      bubbleEl.querySelector('.message-bubble').innerHTML = marked.parse(fullResponse);
    }

    // 推論完了 → インジケータ除去・ストリーム追跡解除
    inferringSessions.delete(sentSessionId || 'new');
    activeStreams.delete(sentSessionId || 'new');
    if (newSessionId) {
      inferringSessions.delete('new');
      activeStreams.delete('new');
    }
    // SSE中にスキップされたwatching_mtime変化を次のポーリングで拾うためリセット
    lastWatchingMtime = 0;
    stopBtn.style.display = 'none';
    sendBtn.style.display = '';
    currentStreamAbort = null;
    await loadSessions();

    if (thinkingEl.parentNode) {
      thinkingEl._stopTimer?.();
      thinkingEl.remove();
    }

  } catch (e) {
    // SSEストリーム切断（中断含む） — ストリーム追跡は解除し、インジケーターはポーリングに委ねる
    activeStreams.delete(sentSessionId || 'new');
    lastWatchingMtime = 0;
    stopBtn.style.display = 'none';
    sendBtn.style.display = '';
    currentStreamAbort = null;
    await loadSessions();
  }
}

function appendMessage(role, content) {
  const container = getSessionContainer(currentSessionId);
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
  const container = getSessionContainer(currentSessionId);
  const div = document.createElement('div');
  div.className = 'thinking-indicator';

  const timerEl = document.createElement('span');
  timerEl.className = 'thinking-timer';
  timerEl.textContent = '0s';
  div.innerHTML = '<div class="spinner"></div> 応答待ち ';
  div.appendChild(timerEl);

  const startTime = Date.now();
  const intervalId = setInterval(() => {
    if (!div.parentNode) { clearInterval(intervalId); return; }
    timerEl.textContent = `${Math.floor((Date.now() - startTime) / 1000)}s`;
  }, 1000);
  // 削除時に自動でタイマーを止める
  div._stopTimer = () => clearInterval(intervalId);

  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

function appendToolUse(description) {
  const container = getSessionContainer(currentSessionId);
  const div = document.createElement('div');
  div.className = 'tool-use-notice';
  div.innerHTML = `<span class="icon">&#9881;</span> ${escapeHtml(description)}`;
  container.appendChild(div);
}

// ============================================================
// セッション DOM キャッシュ
// ============================================================

function getSessionContainer(sessionId) {
  const key = sessionId || 'new';
  if (!sessionDomCache[key]) {
    const el = document.createElement('div');
    el.className = 'chat-messages';
    el.style.display = 'none';
    document.getElementById('chat-messages-host').appendChild(el);
    sessionDomCache[key] = { el, stale: true };
  }
  return sessionDomCache[key].el;
}

function activateSessionContainer(sessionId) {
  // 全コンテナを非表示にして、対象セッションのコンテナだけ表示
  Object.values(sessionDomCache).forEach(c => { c.el.style.display = 'none'; });
  const el = getSessionContainer(sessionId);
  el.style.display = '';
  return el;
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
  const reportsContent = document.getElementById('reports-tab-content');
  const chatPane = document.getElementById('chat-pane');
  const settingsPane = document.getElementById('settings-pane');

  // 中央ペインを切り替え
  chatContent.style.display = 'none';
  settingsContent.classList.remove('visible');
  tasksContent.style.display = 'none';
  reportsContent.style.display = 'none';

  // 右ペインを切り替え
  settingsPane.classList.remove('visible');

  if (tabName === 'chat') {
    chatContent.style.display = 'flex';
    chatPane.classList.remove('hidden');
  } else if (tabName === 'tasks') {
    tasksContent.style.display = 'flex';
    if (!currentSessionId) chatPane.classList.add('hidden');
  } else if (tabName === 'reports') {
    reportsContent.style.display = 'flex';
    if (!currentSessionId) chatPane.classList.add('hidden');
    loadReports();
  } else if (tabName === 'settings') {
    settingsContent.classList.add('visible');
    chatPane.classList.add('hidden');
    settingsPane.classList.add('visible');
    loadSettingsData();
  }
}

// ============================================================
// ファイルブラウザ
// ============================================================

let fileBrowserPath = '';  // 現在表示中のディレクトリパス（ルートからの相対）

async function loadReports() {
  fileBrowserPath = '';
  await renderFileDir('');
}

async function renderFileDir(dirPath) {
  fileBrowserPath = dirPath;
  if (!currentAgentId) return;
  const entryList = document.getElementById('file-entry-list');
  const breadcrumb = document.getElementById('file-breadcrumb');
  entryList.innerHTML = '<div style="padding:8px; color:var(--text-muted); font-size:13px;">読み込み中...</div>';

  // パンくずリスト更新
  const parts = dirPath ? dirPath.split('/') : [];
  let crumbs = `<span class="file-breadcrumb-item${dirPath === '' ? ' current' : ''}" data-path="">ルート</span>`;
  parts.forEach((part, i) => {
    const p = parts.slice(0, i + 1).join('/');
    crumbs += `<span class="file-breadcrumb-sep">/</span>`;
    crumbs += `<span class="file-breadcrumb-item${i === parts.length - 1 ? ' current' : ''}" data-path="${escapeHtml(p)}">${escapeHtml(part)}</span>`;
  });
  breadcrumb.innerHTML = crumbs;
  breadcrumb.querySelectorAll('.file-breadcrumb-item:not(.current)').forEach(el => {
    el.addEventListener('click', () => renderFileDir(el.dataset.path));
  });

  try {
    const url = `${API}/agents/${currentAgentId}/reports?path=${encodeURIComponent(dirPath)}`;
    const data = await fetch(url).then(r => r.json());
    const { dirs, files } = data;

    if (!dirs.length && !files.length) {
      entryList.innerHTML = '<div style="padding:8px; color:var(--text-muted); font-size:13px;">空のディレクトリ</div>';
      return;
    }

    let html = '';
    dirs.forEach(d => {
      const dd = new Date(d.mtime * 1000);
      const ddStr = dd.toLocaleDateString('ja-JP') + ' ' + dd.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
      html += `<div class="file-entry dir" data-path="${escapeHtml(d.path)}">
        <div class="file-entry-row1"><span class="file-entry-icon">📁</span><span class="file-entry-name">${escapeHtml(d.name)}</span></div>
        <div class="file-entry-meta">フォルダ · ${ddStr}</div>
      </div>`;
    });
    files.forEach(f => {
      const isMd = f.is_md;
      const isImage = f.is_image;
      const ext = f.name.split('.').pop().toLowerCase();
      const isHtml = ext === 'html';
      const cls = isMd ? 'file-md' : isImage ? 'file-img' : isHtml ? 'file-html' : 'file-other';
      const icon = isMd ? '📄' : isImage ? '🖼️' : isHtml ? '🌐' : '🔒';
      const sizeStr = f.size < 1024 ? `${f.size}B` : `${(f.size / 1024).toFixed(1)}KB`;
      const d = new Date(f.mtime * 1000);
      const dateStr = d.toLocaleDateString('ja-JP') + ' ' + d.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
      const meta = `${sizeStr} · ${dateStr}`;
      const row2 = f.preview
        ? `<div class="file-entry-preview">${escapeHtml(f.preview)}</div><div class="file-entry-meta">${meta}</div>`
        : `<div class="file-entry-meta">${meta}</div>`;
      html += `<div class="file-entry ${cls}" data-path="${escapeHtml(f.path)}">
        <div class="file-entry-row1"><span class="file-entry-icon">${icon}</span><span class="file-entry-name">${escapeHtml(f.name)}</span></div>
        ${row2}
      </div>`;
    });
    entryList.innerHTML = html;

    entryList.querySelectorAll('.file-entry.dir').forEach(el => {
      el.addEventListener('click', () => renderFileDir(el.dataset.path));
    });
    entryList.querySelectorAll('.file-entry.file-md').forEach(el => {
      el.addEventListener('click', () => openReport(el.dataset.path));
    });
    entryList.querySelectorAll('.file-entry.file-img').forEach(el => {
      el.addEventListener('click', () => openImageFile(el.dataset.path));
    });
    entryList.querySelectorAll('.file-entry.file-html').forEach(el => {
      el.addEventListener('click', () => {
        const pathParts = el.dataset.path.split('/');
        const encoded = pathParts.map(encodeURIComponent).join('/');
        window.open(`${API}/agents/${currentAgentId}/reports/${encoded}`, '_blank');
      });
    });
  } catch (e) {
    entryList.innerHTML = '<div style="padding:8px; color:var(--danger); font-size:13px;">読み込みエラー</div>';
  }
}

async function openReport(filepath) {
  const detailPane = document.getElementById('report-detail-pane');
  const listView = document.getElementById('report-list-view');
  const titleEl = document.getElementById('report-detail-title');
  const bodyEl = document.getElementById('report-detail-body');

  const name = filepath.split('/').pop();
  titleEl.textContent = name;
  bodyEl.innerHTML = '<div style="color:var(--text-muted);">読み込み中...</div>';
  listView.style.display = 'none';
  detailPane.style.display = 'flex';

  try {
    const pathParts = filepath.split('/');
    const encoded = pathParts.map(encodeURIComponent).join('/');
    const text = await fetch(`${API}/agents/${currentAgentId}/reports/${encoded}`).then(r => r.text());
    if (typeof marked !== 'undefined') {
      bodyEl.innerHTML = marked.parse(text);
    } else {
      bodyEl.innerHTML = `<pre style="white-space:pre-wrap; font-size:13px;">${escapeHtml(text)}</pre>`;
    }
  } catch (e) {
    bodyEl.innerHTML = '<div style="color:var(--danger);">読み込みエラー</div>';
  }
}

function openImageFile(filepath) {
  const detailPane = document.getElementById('report-detail-pane');
  const listView = document.getElementById('report-list-view');
  const titleEl = document.getElementById('report-detail-title');
  const bodyEl = document.getElementById('report-detail-body');

  const name = filepath.split('/').pop();
  titleEl.textContent = name;
  const pathParts = filepath.split('/');
  const encoded = pathParts.map(encodeURIComponent).join('/');
  const url = `${API}/agents/${currentAgentId}/reports/${encoded}`;
  bodyEl.innerHTML = `<div style="text-align:center; padding:8px;"><img src="${url}" style="max-width:100%; height:auto; border-radius:6px;" alt="${escapeHtml(name)}"></div>`;
  listView.style.display = 'none';
  detailPane.style.display = 'flex';
}

function initReports() {
  document.getElementById('report-back-btn').addEventListener('click', () => {
    document.getElementById('report-detail-pane').style.display = 'none';
    document.getElementById('report-list-view').style.display = 'flex';
  });
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

  // systemエージェント以外のとき登録解除ボタンを表示
  const dangerZone = document.getElementById('danger-zone');
  dangerZone.style.display = currentAgentId === 'system' ? 'none' : 'block';
}

async function loadSchedulerLogs() {
  const resp = await fetch(`${API}/scheduler/logs`);
  if (!resp.ok) return;
  const logs = await resp.json();
  const el = document.getElementById('scheduler-log-list');
  if (!el) return;
  if (logs.length === 0) {
    el.innerHTML = '<div class="scheduler-log-empty">実行履歴なし</div>';
    return;
  }
  el.innerHTML = logs.map(log => {
    const dt = new Date(log.timestamp);
    const dateStr = `${dt.getMonth()+1}/${dt.getDate()} ${String(dt.getHours()).padStart(2,'0')}:${String(dt.getMinutes()).padStart(2,'0')}`;
    const progress = log.total_after > 0 ? `${log.checked_after}/${log.total_after}` : '—';
    const sessionShort = log.session_id ? log.session_id.slice(0, 8) : '—';
    const stepsHtml = (log.completed_steps && log.completed_steps.length > 0)
      ? log.completed_steps.map(s => `<div class="slog-step">✓ ${escapeHtml(s)}</div>`).join('')
      : `<span class="slog-unchanged">${escapeHtml(log.current_step || '—')} の作業中</span>`;
    const errHtml = log.error ? `<div class="slog-error">${escapeHtml(log.error)}</div>` : '';
    const sessionLink = log.session_id
      ? `<a class="slog-link" data-session-id="${log.session_id}">session: ${sessionShort}</a>`
      : '—';
    return `<div class="scheduler-log-entry${log.error ? ' slog-has-error' : ''}">
      <div class="slog-header">
        <span class="slog-time">${dateStr}</span>
        <span class="slog-agent">${escapeHtml(log.agent_name || log.agent_id || '—')}</span>
        <a class="slog-link slog-title" data-task-id="${log.task_id}">${escapeHtml(log.task_title || log.task_id)}</a>
        <span class="slog-progress">${progress}</span>
      </div>
      <div class="slog-steps">${stepsHtml}</div>
      <div class="slog-meta">${sessionLink}</div>
      ${errHtml}
    </div>`;
  }).join('');

  // イベント委譲
  el.onclick = async (e) => {
    const taskLink = e.target.closest('[data-task-id]');
    if (taskLink) {
      closeSchedulerLog();
      switchTab('tasks');
      if (!tasksCache[taskLink.dataset.taskId]) await loadTasks();
      selectTask(taskLink.dataset.taskId);
      return;
    }
    const sessionLink = e.target.closest('[data-session-id]');
    if (sessionLink) {
      document.getElementById('chat-pane').classList.remove('hidden');
      await selectSession(sessionLink.dataset.sessionId);
    }
  };
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

  // エージェント追加フォーム
  initAddAgent();

  // エージェント登録解除
  document.getElementById('delete-agent-btn').addEventListener('click', async () => {
    if (!currentAgentId || currentAgentId === 'system') return;
    const agent = agents.find(a => a.id === currentAgentId);
    const name = agent ? agent.name : currentAgentId;
    if (!confirm(`エージェント "${name}" の登録を解除しますか？\nプロジェクトデータは削除されません。`)) return;
    const resp = await fetch(`${API}/agents/${currentAgentId}`, { method: 'DELETE' });
    if (!resp.ok) {
      const err = await resp.json();
      showToast(err.detail || 'エラーが発生しました');
      return;
    }
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

  // チャットメニュー開閉
  const chatMenuBtn = document.getElementById('chat-menu-btn');
  const chatMenu = document.getElementById('chat-menu');
  chatMenuBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    chatMenu.classList.toggle('open');
  });
  document.addEventListener('click', () => chatMenu.classList.remove('open'));

  // メニュー: タイトル編集
  document.getElementById('btn-edit-title').addEventListener('click', () => {
    chatMenu.classList.remove('open');
    if (!currentAgentId || !currentSessionId) return;
    const newTitle = prompt('会話タイトルを入力', currentSessionTitle);
    if (newTitle === null) return;
    saveSessionTitle(newTitle);
  });

  // メニュー: CLI起動
  document.getElementById('btn-cli').addEventListener('click', async () => {
    chatMenu.classList.remove('open');
    if (!currentAgentId) return;
    await fetch(`${API}/agents/${currentAgentId}/cli`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: currentSessionId }),
    });
  });

  // メニュー: 非表示
  document.getElementById('btn-hide').addEventListener('click', async () => {
    chatMenu.classList.remove('open');
    if (!currentAgentId || !currentSessionId) return;
    if (!confirm('この会話セッションをリストから非表示にしますか？')) return;
    await fetch(`${API}/agents/${currentAgentId}/sessions/${currentSessionId}/hide`, {
      method: 'POST',
    });
    currentSessionId = null;
    clearChat();
    await loadSessions();
  });

  // メニュー: 完全削除
  document.getElementById('btn-delete-session').addEventListener('click', async () => {
    chatMenu.classList.remove('open');
    if (!currentAgentId || !currentSessionId) return;
    if (!confirm('この会話セッションを完全削除しますか？\nメタデータと会話ログが削除されます。この操作は取り消せません。')) return;
    const sid = currentSessionId;
    currentSessionId = null;
    clearChat();
    await fetch(`${API}/agents/${currentAgentId}/sessions/${sid}`, { method: 'DELETE' });
    await loadSessions();
  });

  // 新規会話
  document.getElementById('new-chat-btn').addEventListener('click', () => {
    currentSessionId = null;
    clearChat();
    activateSessionContainer(null); // 新規入力用の空コンテナを表示
    document.querySelectorAll('.conversation-item').forEach(el => el.classList.remove('active'));
    // モバイル: チャット画面に切り替え
    if (isMobile()) {
      document.querySelector('.layout').classList.add('mobile-chat-active');
    }
  });

  // タイトルクリック編集（メニューからも可能）
  document.getElementById('chat-title').addEventListener('click', () => {
    if (!currentAgentId || !currentSessionId) return;
    const newTitle = prompt('会話タイトルを入力', currentSessionTitle);
    if (newTitle === null) return;
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
// タスク管理
// ============================================================

let currentTaskId = null;
let showingDoneHistory = false;
let taskContextMenuId = null;

// キャッシュ
let tasksCache = {};         // task_id → task
let taskExecutionOrder = []; // 承認済みタスクの実行順序

async function loadTasks() {
  if (!currentAgentId) return;
  try {
    const resp = await fetch(`${API}/agents/${currentAgentId}/tasks`);
    if (!resp.ok) return;
    const data = await resp.json();
    tasksCache = {};
    (data.tasks || []).forEach(t => { tasksCache[t.task_id] = t; });
    taskExecutionOrder = data.order || [];
    renderTaskList();
    if (currentTaskId && tasksCache[currentTaskId]) {
      // 編集中（textarea表示中）は再レンダリングしない
      if (!document.getElementById('task-edit-textarea')) {
        renderTaskDetail(currentTaskId);
      }
    }
  } catch (e) {
    console.error('タスク読み込みエラー', e);
  }
}

function initTasks() {
  loadTasks();

  document.addEventListener('click', (e) => {
    const menu = document.getElementById('task-context-menu');
    if (!menu.contains(e.target) && !e.target.classList.contains('task-more-btn')) {
      menu.classList.remove('visible');
    }
  });

  document.getElementById('ctx-talk').addEventListener('click', async () => {
    document.getElementById('task-context-menu').classList.remove('visible');
    if (!taskContextMenuId) return;
    await openTalkSession(taskContextMenuId);
  });

  document.getElementById('ctx-work').addEventListener('click', async () => {
    document.getElementById('task-context-menu').classList.remove('visible');
    if (!taskContextMenuId) return;
    const task = tasksCache[taskContextMenuId];
    await startWorkSession(taskContextMenuId, `タスク「${task.title}」について1ステップ作業してください。`);
  });

  document.getElementById('ctx-edit').addEventListener('click', () => {
    document.getElementById('task-context-menu').classList.remove('visible');
    enterTaskEditMode();
  });

  document.getElementById('ctx-force-done').addEventListener('click', async () => {
    document.getElementById('task-context-menu').classList.remove('visible');
    if (!taskContextMenuId) return;
    await fetch(`${API}/agents/${currentAgentId}/tasks/${taskContextMenuId}/force-done`, { method: 'POST' });
    await loadTasks();
  });

  document.getElementById('ctx-delete').addEventListener('click', async () => {
    document.getElementById('task-context-menu').classList.remove('visible');
    if (!taskContextMenuId) return;
    const task = tasksCache[taskContextMenuId];
    if (!confirm(`「${task?.title}」を削除しますか？`)) return;
    await fetch(`${API}/agents/${currentAgentId}/tasks/${taskContextMenuId}`, { method: 'DELETE' });
    if (currentTaskId === taskContextMenuId) backToTaskList();
    await loadTasks();
  });

}


function renderTaskList() {
  const list = document.getElementById('task-list');
  let html = '';

  let queueIdx = 0;
  taskExecutionOrder.forEach((id) => {
    const task = tasksCache[id];
    if (!task || task.approval !== 'approved' || task.phase === 'done') return;
    queueIdx++;
    const active = id === currentTaskId ? ' active' : '';
    const indicator = task.phase === 'doing'
      ? '<span class="doing-indicator"><span class="dot"></span>実行中</span>'
      : '';
    html += `
      <div class="task-item draggable${active}" data-task-id="${id}" data-row-id="${id}" draggable="true">
        <div class="task-item-header">
          <span class="drag-handle">&#x2630;</span>
          <div class="task-title-group">
            <span class="task-item-title">${escapeHtml(task.title)}</span>
            ${indicator}
          </div>
        </div>
        <div class="task-item-meta"><span>${formatTaskDate(task.created)}</span></div>
      </div>`;
  });

  if (taskExecutionOrder.length > 0) {
    html += `<div class="task-drop-end" id="task-drop-end"></div>`;
  }

  Object.values(tasksCache).filter(t => t.approval !== 'approved' && t.phase !== 'done').forEach(task => {
    const active = task.task_id === currentTaskId ? ' active' : '';
    html += `
      <div class="task-item${active}" data-task-id="${task.task_id}">
        <div class="task-item-header">
          <div class="task-title-group">
            <span class="task-item-title">${escapeHtml(task.title)}</span>
          </div>
        </div>
        <div class="task-item-meta"><span class="badge badge-pending">承認待ち</span><span>${formatTaskDate(task.created)}</span></div>
      </div>`;
  });


  const doneTasks = Object.values(tasksCache)
    .filter(t => t.phase === 'done' && t.approval === 'approved')
    .sort((a, b) => b.created.localeCompare(a.created));

  if (doneTasks.length > 0) {
    const latest = doneTasks[0];
    const active = latest.task_id === currentTaskId ? ' active' : '';
    html += `
      <div class="task-item task-last-done${active}" data-task-id="${latest.task_id}">
        <div class="task-item-header">
          <div class="task-title-group">
            <span class="task-item-title">${escapeHtml(latest.title)}</span>
          </div>
        </div>
        <div class="task-item-meta"><span class="badge badge-done">done</span><span>${formatTaskDate(latest.created)} 完了</span></div>
      </div>`;

    if (doneTasks.length > 1) {
      const histItems = doneTasks.slice(1).map(task => {
        const a = task.task_id === currentTaskId ? ' active' : '';
        return `
          <div class="task-item${a}" data-task-id="${task.task_id}">
            <div class="task-item-header">
              <div class="task-title-group">
                <span class="task-item-title">${escapeHtml(task.title)}</span>
              </div>
            </div>
            <div class="task-item-meta"><span class="badge badge-done">done</span><span>${formatTaskDate(task.created)} 完了</span></div>
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

  list.innerHTML = html;

  list.querySelectorAll('.task-item[data-task-id]').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('.drag-handle')) return;
      selectTask(el.dataset.taskId);
    });
  });

  const toggleEl = document.getElementById('task-done-toggle');
  if (toggleEl) {
    toggleEl.addEventListener('click', () => {
      showingDoneHistory = !showingDoneHistory;
      renderTaskList();
    });
  }

  initTaskDragDrop();
}

function selectTask(taskId) {
  currentTaskId = taskId;
  renderTaskList();
  document.getElementById('task-list-view').style.display = 'none';
  const detailPane = document.getElementById('task-detail-pane');
  detailPane.style.display = 'flex';
  renderTaskDetail(taskId);
}

function backToTaskList() {
  currentTaskId = null;
  document.getElementById('task-list-view').style.display = '';
  document.getElementById('task-detail-pane').style.display = 'none';
  renderTaskList();
}

async function renderTaskDetail(taskId) {
  const task = tasksCache[taskId];
  if (!task) return;

  if (sessions.length === 0) await loadSessions();

  const headerEl = document.getElementById('task-detail-header');
  let approvalHtml = '';
  if (task.approval === 'pending') {
    approvalHtml = `
      <button class="approval-btn approve" data-approve="${taskId}">承認</button>
`;
  } else if (task.approval === 'approved') {
    const dt = task.approved_at
      ? new Date(task.approved_at).toLocaleString('ja-JP', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
      : '';
    approvalHtml = `<div class="approval-status-badge approved">✓ 承認済み${dt ? ' (' + dt + ')' : ''}</div>`;
  }

  const phaseLabel = { draft: '未着手', doing: '実行中', done: '完了' }[task.phase] ?? task.phase;
  const phaseBadge = task.phase === 'doing'
    ? '<span class="doing-indicator"><span class="dot"></span>実行中</span>'
    : task.phase === 'done'
    ? '<span class="badge badge-done">完了</span>'
    : '';

  headerEl.innerHTML = `
    <div class="task-detail-header-left">
      <button class="task-back-btn" id="task-detail-back">←</button>
    </div>
    <div class="task-detail-actions">
      <button class="task-more-btn" data-more="${taskId}" title="その他">&#x22EF;</button>
    </div>`;

  headerEl.querySelector('#task-detail-back').addEventListener('click', backToTaskList);

  headerEl.querySelector(`[data-more="${taskId}"]`)?.addEventListener('click', (e) => {
    e.stopPropagation();
    taskContextMenuId = taskId;
    const menu = document.getElementById('task-context-menu');
    const rect = e.currentTarget.getBoundingClientRect();
    menu.style.top = (rect.bottom + 4) + 'px';
    menu.style.right = (window.innerWidth - rect.right) + 'px';
    menu.style.left = '';
    menu.classList.toggle('visible');
    document.getElementById('ctx-force-done').style.opacity = task.phase === 'done' ? '0.4' : '';
    document.getElementById('ctx-force-done').style.pointerEvents = task.phase === 'done' ? 'none' : '';
  });

  const bodyEl = document.getElementById('task-detail-body');
  const created = new Date(task.created).toLocaleString('ja-JP', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  const bodyHtml = task.body ? marked.parse(task.body) : '';

  const totalSteps = (task.body.match(/- \[[ x]\]/g) || []).length;
  const doneSteps = (task.body.match(/- \[x\]/g) || []).length;
  const progress = totalSteps > 0 ? Math.round(doneSteps / totalSteps * 100) : null;
  let segmentStyle = '';
  if (totalSteps > 1) {
    const stops = Array.from({ length: totalSteps - 1 }, (_, i) => {
      const pct = Math.round((i + 1) / totalSteps * 100);
      return `transparent ${pct}%, var(--bg-primary) ${pct}%, var(--bg-primary) calc(${pct}% + 3px), transparent calc(${pct}% + 3px)`;
    }).join(', ');
    segmentStyle = `style="background-image: linear-gradient(to right, ${stops})"`;
  }
  const progressBar = progress !== null ? `
    <div class="task-progress-bar">
      <div class="task-progress-fill" style="width:${progress}%"></div>
      <div class="task-progress-segments" ${segmentStyle}></div>
    </div>
    <div class="task-progress-label">${doneSteps} / ${totalSteps}</div>` : '';

  bodyEl.innerHTML = `
    ${progressBar}
    <div class="task-detail-title-full">${escapeHtml(task.title)}</div>
    <div class="task-md-content">${bodyHtml}</div>
    <div class="task-meta-bar">
      <div class="task-meta-item"><span class="task-meta-label">フェーズ:</span>${phaseBadge || `<span>${phaseLabel}</span>`}</div>
      <div class="task-meta-item">${approvalHtml || ''}</div>
      <div class="task-meta-item"><span class="task-meta-label">起票:</span><span>${created}</span></div>
      ${task.schedule ? `<div class="task-meta-item"><span class="task-meta-label">スケジュール:</span><span>${escapeHtml(task.schedule)}</span></div>` : ''}
      ${task.talk_session_id ? (() => {
        const s = sessions.find(s => s.session_id === task.talk_session_id);
        return s ? `<div class="task-meta-item"><span class="task-meta-label">相談セッション:</span><a class="task-session-link" data-session-id="${task.talk_session_id}">${escapeHtml(s.title || s.session_id)}</a></div>` : '';
      })() : ''}
      ${task.sessions && task.sessions.length > 0 ? `
        <div class="task-meta-item task-meta-item--col">
          <span class="task-meta-label">作業セッション:</span>
          ${task.sessions.map(sid => {
            const s = sessions.find(s => s.session_id === sid);
            return s ? `<a class="task-session-link" data-session-id="${sid}">${escapeHtml(s.title || sid)}</a>` : '';
          }).filter(Boolean).join('')}
        </div>` : ''}
    </div>`;

  bodyEl.querySelectorAll('.task-session-link').forEach(el => {
    el.addEventListener('click', async (e) => {
      const sid = e.currentTarget.dataset.sessionId;
      document.getElementById('chat-pane').classList.remove('hidden');
      await selectSession(sid);
    });
  });

  bodyEl.querySelector(`[data-approve="${taskId}"]`)?.addEventListener('click', async () => {
    await fetch(`${API}/agents/${currentAgentId}/tasks/${taskId}/approve`, { method: 'POST' });
    await loadTasks();
  });

}

function enterTaskEditMode() {
  const task = tasksCache[currentTaskId];
  if (!task) return;

  const bodyEl = document.getElementById('task-detail-body');
  const mdContent = bodyEl.querySelector('.task-md-content');
  if (!mdContent) return;

  mdContent.innerHTML = `
    <textarea class="task-edit-textarea" id="task-edit-textarea">${escapeHtml(task.body || '')}</textarea>
    <div class="task-edit-actions">
      <button class="chat-action-btn" id="task-edit-cancel">キャンセル</button>
      <button class="settings-save-btn" id="task-edit-save">保存</button>
    </div>`;

  document.getElementById('task-edit-cancel').addEventListener('click', () => {
    renderTaskDetail(currentTaskId);
  });

  document.getElementById('task-edit-save').addEventListener('click', async () => {
    const body = document.getElementById('task-edit-textarea').value;
    await fetch(`${API}/agents/${currentAgentId}/tasks/${currentTaskId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ body }),
    });
    await loadTasks();
    showToast('保存しました');
  });
}

function formatTaskDate(iso) {
  const d = new Date(iso);
  return d.toLocaleString('ja-JP', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}


function initTaskDragDrop() {
  let dragRowId = null;

  document.querySelectorAll('.task-item.draggable').forEach(item => {
    item.addEventListener('dragstart', (e) => {
      dragRowId = item.dataset.rowId;
      item.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    });

    item.addEventListener('dragend', () => {
      item.classList.remove('dragging');
      document.querySelectorAll('.task-item.drag-over').forEach(el => el.classList.remove('drag-over'));
      document.getElementById('task-drop-end')?.classList.remove('drag-over');
      dragRowId = null;
    });

    item.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (item.dataset.rowId === dragRowId) return;
      e.dataTransfer.dropEffect = 'move';
      item.classList.add('drag-over');
    });

    item.addEventListener('dragleave', () => item.classList.remove('drag-over'));

    item.addEventListener('drop', async (e) => {
      e.preventDefault();
      item.classList.remove('drag-over');
      if (!dragRowId || item.dataset.rowId === dragRowId) return;
      const fromIdx = taskExecutionOrder.indexOf(dragRowId);
      const toIdx = taskExecutionOrder.indexOf(item.dataset.rowId);
      if (fromIdx < 0 || toIdx < 0) return;
      taskExecutionOrder.splice(fromIdx, 1);
      taskExecutionOrder.splice(toIdx, 0, dragRowId);
      renderTaskList();
      await fetch(`${API}/agents/${currentAgentId}/tasks/order`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order: taskExecutionOrder }),
      });
    });
  });

  const endZone = document.getElementById('task-drop-end');
  if (endZone) {
    endZone.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (!dragRowId) return;
      e.dataTransfer.dropEffect = 'move';
      endZone.classList.add('drag-over');
    });
    endZone.addEventListener('dragleave', () => endZone.classList.remove('drag-over'));
    endZone.addEventListener('drop', async (e) => {
      e.preventDefault();
      endZone.classList.remove('drag-over');
      if (!dragRowId) return;
      const fromIdx = taskExecutionOrder.indexOf(dragRowId);
      if (fromIdx < 0 || fromIdx === taskExecutionOrder.length - 1) return;
      taskExecutionOrder.splice(fromIdx, 1);
      taskExecutionOrder.push(dragRowId);
      renderTaskList();
      await fetch(`${API}/agents/${currentAgentId}/tasks/order`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order: taskExecutionOrder }),
      });
    });
  }
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// ============================================================
// スケジューラー
// ============================================================

function openSchedulerLog() {
  document.querySelector('.sidebar').classList.remove('open');
  const pane = document.getElementById('scheduler-log-pane');
  pane.style.display = 'flex';
  loadSchedulerLogs();
}

function closeSchedulerLog() {
  document.getElementById('scheduler-log-pane').style.display = 'none';
}

function initScheduler() {
  document.getElementById('scheduler-log-btn').addEventListener('click', openSchedulerLog);
  document.getElementById('scheduler-log-back-btn').addEventListener('click', closeSchedulerLog);
  document.getElementById('scheduler-log-refresh-btn').addEventListener('click', loadSchedulerLogs);

  const btn = document.getElementById('scheduler-toggle-btn');
  btn.addEventListener('click', async () => {
    const label = document.getElementById('scheduler-label').textContent;
    const next = label === 'ON' ? 'OFF' : 'ON';
    if (!confirm(`スケジューラーを${next}にしますか？`)) return;
    try {
      const resp = await fetch(`${API}/scheduler/toggle`, { method: 'POST' });
      if (resp.ok) {
        updateSchedulerUI(await resp.json());
      }
    } catch (e) {
      console.error('[SCHEDULER] toggle error:', e);
    }
  });

  // 初回取得 + 30秒ポーリング
  fetchSchedulerStatus();
  setInterval(fetchSchedulerStatus, 30000);
}

async function fetchSchedulerStatus() {
  try {
    const resp = await fetch(`${API}/scheduler/status`);
    if (resp.ok) {
      updateSchedulerUI(await resp.json());
    }
  } catch (_) {}
}

function updateSchedulerUI(data) {
  const btn = document.getElementById('scheduler-toggle-btn');
  const indicator = document.getElementById('scheduler-indicator');
  const label = document.getElementById('scheduler-label');

  if (data.enabled) {
    btn.classList.add('on');
    indicator.classList.remove('off');
    indicator.classList.add('on');
    label.textContent = 'ON';
  } else {
    btn.classList.remove('on');
    indicator.classList.remove('on');
    indicator.classList.add('off');
    label.textContent = 'OFF';
  }
}

// ============================================================
// エージェント追加
// ============================================================

function initAddAgent() {
  const pane = document.getElementById('add-agent-pane');
  const openBtn = document.getElementById('add-agent-btn');
  const cancelBtn = document.getElementById('add-agent-cancel-btn');
  const cancelBtn2 = document.getElementById('add-agent-cancel-btn2');
  const submitBtn = document.getElementById('add-agent-submit-btn');

  function openForm() {
    // フォームをリセット
    document.getElementById('add-agent-name').value = '';
    document.getElementById('add-agent-path').value = '';
    document.getElementById('add-agent-description').value = '';
    document.getElementById('add-agent-cli').value = 'claude';
    document.getElementById('add-agent-model-tier').value = 'deep';
    pane.style.display = 'flex';
  }

  function closeForm() {
    pane.style.display = 'none';
  }

  openBtn.addEventListener('click', openForm);
  cancelBtn.addEventListener('click', closeForm);
  cancelBtn2.addEventListener('click', closeForm);

  submitBtn.addEventListener('click', async () => {
    const name = document.getElementById('add-agent-name').value.trim();
    const path = document.getElementById('add-agent-path').value.trim();
    const description = document.getElementById('add-agent-description').value.trim();
    const cli = document.getElementById('add-agent-cli').value;
    const model_tier = document.getElementById('add-agent-model-tier').value;

    if (!name || !path) {
      showToast('名前とプロジェクトパスは必須です');
      return;
    }

    try {
      const resp = await fetch(`${API}/agents`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, path, description, cli, model_tier }),
      });
      if (!resp.ok) {
        const err = await resp.json();
        showToast(err.detail || 'エラーが発生しました');
        return;
      }
      const newAgent = await resp.json();
      closeForm();
      await loadAgents();
      selectAgent(newAgent.id);
    } catch (e) {
      showToast('通信エラーが発生しました');
    }
  });
}
