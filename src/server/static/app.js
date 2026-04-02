// kobito_agents フロントエンド

const API = '/api';
let currentAgentId = null;
let currentSessionId = null;
let agents = [];
let sessions = [];
let sessionStates = {}; // { sessionId: 'idle' | 'waiting' | 'streaming' }
let sessionModelTiers = {}; // { sessionId: 'deep' | 'quick' }
let activeProcessSessions = new Set(); // 常駐プロセスが稼働中のセッションID
let respondingSessions = new Set();   // バックエンドで応答処理中（ロック取得中）のセッションID
let processStatusInterval = null;
let lastDirMtime = 0;     // セッションディレクトリの前回mtime
let lastWatchingMtime = 0; // 表示中セッションJSONLの前回mtime
let lastStartupId = null;  // サーバー起動IDキャッシュ（変化でリロード検知）
const sessionDomCache = {}; // { sessionId: { el: HTMLElement, stale: boolean } } セッションDOMキャッシュ

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
  // タスク詳細を閉じてタスク一覧を読み込む
  backToTaskList();
  await loadTasks();
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

    // バックエンドで応答が完了したセッションの残留インジケーターを除去
    // （SSEストリームが切断等で thinkingEl が残ったまま responding=false になった場合）
    const prevResponding = respondingSessions;

    // プロセス稼働ドット更新
    activeProcessSessions = new Set(data.active);
    document.querySelectorAll('.conversation-item').forEach(el => {
      const sid = el.dataset.sessionId;
      el.classList.toggle('process-active', activeProcessSessions.has(sid));
    });

    // バックエンド応答中セッションを確認し、フロントが見逃しているものを補完
    respondingSessions = new Set(data.responding);
    // 前回 responding だったが今回なくなったセッション → 残留 thinkingEl を除去
    prevResponding.forEach(sid => {
      if (!respondingSessions.has(sid) && !sessionStates[sid]) {
        const container = getSessionContainer(sid);
        const ind = container.querySelector('.thinking-indicator');
        if (ind) { ind._stopTimer?.(); ind.remove(); }
      }
    });
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
        // 現在表示中のセッションなら、会話ウィンドウにも thinkingEl を復元
        if (sid === currentSessionId) {
          const container = getSessionContainer(sid);
          if (!container.querySelector('.thinking-indicator')) {
            appendThinking();
          }
        }
      }
    });

    // セッションディレクトリ変化 → 一覧を再取得 + 現在表示中以外のキャッシュを無効化
    if (data.dir_mtime && data.dir_mtime !== lastDirMtime) {
      if (lastDirMtime !== 0) {
        // 現在表示中以外のキャッシュを stale にする（表示中は後続の watching_mtime で対処）
        Object.entries(sessionDomCache).forEach(([key, cache]) => {
          if (key !== currentSessionId) cache.stale = true;
        });
        await loadSessions();
      }
      lastDirMtime = data.dir_mtime;
    }

    // 表示中セッション変化 → 履歴を強制再取得
    // 再起動待機中(restart-waiting)も含む。アクティブストリーミング中(streaming/waiting)は除く
    if (data.watching_mtime && data.watching_mtime !== lastWatchingMtime) {
      const st = sessionStates[currentSessionId];
      const canReload = !st || st === 'restart-waiting';
      if (lastWatchingMtime !== 0 && canReload) {
        delete sessionStates[currentSessionId]; // 再起動待機状態を解除
        await loadSessionHistory(currentSessionId, true);
        await loadSessions();
      }
      lastWatchingMtime = data.watching_mtime;
    }
  } catch (_) {}
}

function startProcessStatusPolling() {
  if (processStatusInterval) clearInterval(processStatusInterval);
  activeProcessSessions = new Set();
  respondingSessions = new Set();
  const poll = () => { pollProcessStatus(); loadTasks(); };
  poll();
  processStatusInterval = setInterval(poll, 5000);
}

// ============================================================
// セッション一覧
// ============================================================

async function loadSessions() {
  if (!currentAgentId) return;
  // フェッチ開始時点のsessionStatesスナップショットを取る（非同期競合対策）
  const stateSnapshot = { ...sessionStates };
  const resp = await fetch(`${API}/agents/${currentAgentId}/sessions`);
  sessions = await resp.json();
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
    // sessionStates（フロント管理）またはrespondingSessions（バックエンド実態）のどちらかが応答中なら表示
    const isResponding = state === 'waiting' || state === 'streaming' || state === 'restart-waiting' || respondingSessions.has(s.session_id);
    const statusHtml = isResponding
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

  // キャッシュが有効 (stale でない) 場合はメッセージ再取得をスキップ
  if (!force && cache && !cache.stale) {
    cache.el.scrollTop = cache.el.scrollHeight;
    document.getElementById('chat-title').textContent = currentSessionTitle || '';
    return;
  }

  const resp = await fetch(`${API}/agents/${currentAgentId}/sessions/${sessionId}`);
  const messages = await resp.json();
  renderMessages(messages, sessionId);

  // セッション一覧からタイトルを取得
  const sessResp = await fetch(`${API}/agents/${currentAgentId}/sessions`);
  const sessions = await sessResp.json();
  const session = sessions.find(s => s.session_id === sessionId);
  currentSessionTitle = session?.title || '';

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
  await sendMessage();

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

  currentSessionId = null;
  clearChat();
  activateSessionContainer(null);
  document.querySelectorAll('.conversation-item').forEach(el => el.classList.remove('active'));
  switchTab('chat');

  const input = document.getElementById('chat-input');
  input.value = message;
  await sendMessage();

  if (currentSessionId) {
    await fetch(`${API}/agents/${currentAgentId}/tasks/${taskId}/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: currentSessionId }),
    });
  }
}

async function sendMessage() {
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

    // エラーの場合
    if (streamError) {
      const isRestartError = streamError.includes('再起動');
      const resolvedSid = newSessionId || sentSessionId;

      if (isRestartError) {
        // サーバー再起動エラー: CLIサブプロセスは生き続けている可能性があるため
        // インジケーターを消さず継続待機（watching_mtime でファイル変化を検知して自動解除）
        showToast('サーバーが再起動されました。応答を受信待ちです...');
        // 'restart-waiting' 状態にする（session一覧表示は維持、watching_mtimeで再取得可能にする）
        sessionStates[sentSessionId || 'new'] = 'restart-waiting';
        await loadSessions();
        // thinkingEl はそのまま残す（ポーリングの watching_mtime 検知で自動解除）
      } else {
        // 通常エラー: インジケーターを消してエラーメッセージを表示
        if (thinkingEl.parentNode) { thinkingEl._stopTimer?.(); thinkingEl.remove(); }
        if (isViewingThisSession()) {
          appendMessage('assistant', `エラー: ${streamError}`);
        }
        delete sessionStates[sentSessionId || 'new'];
        if (newSessionId) delete sessionStates['new'];
        if (resolvedSid) {
          await loadSessions();
          if (currentAgentId === sentAgentId && currentSessionId === resolvedSid) {
            await loadSessionHistory(resolvedSid);
          }
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
      thinkingEl._stopTimer?.();
      thinkingEl.remove();
    }

  } catch (e) {
    if (thinkingEl.parentNode) {
      thinkingEl._stopTimer?.();
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
  const chatPane = document.getElementById('chat-pane');
  const settingsPane = document.getElementById('settings-pane');
  const taskDetailPane = document.getElementById('task-detail-pane');

  // 中央ペインを切り替え
  chatContent.style.display = 'none';
  settingsContent.classList.remove('visible');
  tasksContent.style.display = 'none';

  // 右ペインを切り替え
  settingsPane.classList.remove('visible');

  if (tabName === 'chat') {
    chatContent.style.display = '';
    chatPane.classList.remove('hidden');
  } else if (tabName === 'tasks') {
    tasksContent.style.display = 'flex';
    // セッション表示中はチャットペインをそのまま維持
    if (!currentSessionId) chatPane.classList.add('hidden');
  } else if (tabName === 'settings') {
    chatPane.classList.add('hidden');
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
    activateSessionContainer(null); // 新規入力用の空コンテナを表示
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
// タスク管理
// ============================================================

let currentTaskId = null;
let showingDoneHistory = false;
let taskContextMenuId = null;
let pendingRejectTaskId = null;

// キャッシュ
let tasksCache = {};         // task_id → task
let taskExecutionOrder = []; // 承認済みタスクの実行順序

async function loadTasks() {
  if (!currentAgentId) return;
  try {
    const data = await fetch(`${API}/agents/${currentAgentId}/tasks`).then(r => r.json());
    tasksCache = {};
    data.tasks.forEach(t => { tasksCache[t.task_id] = t; });
    taskExecutionOrder = data.order || [];
    renderTaskList();
    updatePendingBadge();
    if (currentTaskId && tasksCache[currentTaskId]) {
      renderTaskDetail(currentTaskId);
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

  document.getElementById('reject-cancel').addEventListener('click', hideRejectDialog);
  document.getElementById('reject-confirm').addEventListener('click', async () => {
    if (!pendingRejectTaskId) return;
    const reason = document.getElementById('reject-reason').value;
    await fetch(`${API}/agents/${currentAgentId}/tasks/${pendingRejectTaskId}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    });
    const tid = pendingRejectTaskId;
    hideRejectDialog();
    await loadTasks();
    if (currentTaskId === tid) renderTaskDetail(tid);
  });
}

function updatePendingBadge() {
  const count = Object.values(tasksCache).filter(t => t.approval === 'pending').length;
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

  let queueIdx = 0;
  taskExecutionOrder.forEach((id) => {
    const task = tasksCache[id];
    if (!task || task.approval !== 'approved') return;
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

  Object.values(tasksCache).filter(t => t.approval === 'pending').forEach(task => {
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

  Object.values(tasksCache).filter(t => t.approval === 'rejected').forEach(task => {
    const active = task.task_id === currentTaskId ? ' active' : '';
    html += `
      <div class="task-item${active}" data-task-id="${task.task_id}" style="opacity:0.4;">
        <div class="task-item-header">
          <div class="task-title-group">
            <span class="task-item-title">${escapeHtml(task.title)}</span>
          </div>
        </div>
        <div class="task-item-meta"><span class="badge" style="background:#f8514933;color:var(--danger);border:1px solid var(--danger);">却下</span><span>${formatTaskDate(task.created)}</span></div>
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

function renderTaskDetail(taskId) {
  const task = tasksCache[taskId];
  if (!task) return;

  const headerEl = document.getElementById('task-detail-header');
  let approvalHtml = '';
  if (task.approval === 'pending') {
    approvalHtml = `
      <button class="approval-btn approve" data-approve="${taskId}">承認</button>
      <button class="approval-btn reject" data-reject="${taskId}">却下</button>`;
  } else if (task.approval === 'approved') {
    const dt = task.approved_at
      ? new Date(task.approved_at).toLocaleString('ja-JP', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
      : '';
    approvalHtml = `<div class="approval-status-badge approved">✓ 承認済み${dt ? ' (' + dt + ')' : ''}</div>`;
  } else if (task.approval === 'rejected') {
    const dt = task.rejected_at
      ? new Date(task.rejected_at).toLocaleString('ja-JP', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
      : '';
    approvalHtml = `<div class="approval-status-badge rejected">✗ 却下${dt ? ' (' + dt + ')' : ''}</div>`;
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
    </div>`;

  bodyEl.querySelector('.task-session-link')?.addEventListener('click', async (e) => {
    const sid = e.currentTarget.dataset.sessionId;
    document.getElementById('chat-pane').classList.remove('hidden');
    await selectSession(sid);
  });

  bodyEl.querySelector(`[data-approve="${taskId}"]`)?.addEventListener('click', async () => {
    await fetch(`${API}/agents/${currentAgentId}/tasks/${taskId}/approve`, { method: 'POST' });
    await loadTasks();
  });

  bodyEl.querySelector(`[data-reject="${taskId}"]`)?.addEventListener('click', () => {
    pendingRejectTaskId = taskId;
    showRejectDialog();
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
  });
}

function formatTaskDate(iso) {
  const d = new Date(iso);
  return d.toLocaleString('ja-JP', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
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
