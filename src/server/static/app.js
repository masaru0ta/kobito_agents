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

async function loadAgents({ autoSelect = true } = {}) {
  const resp = await fetch(`${API}/agents`);
  agents = await resp.json();
  renderAgents();
  if (autoSelect && agents.length > 0) {
    selectAgent(agents[0].id);
  }
}

function renderAgents() {
  const list = document.getElementById('agent-list');
  list.innerHTML = agents.map(a => {
    const avatarHtml = a.thumbnail_url
      ? `<img src="${a.thumbnail_url}" class="agent-avatar-img" alt="">`
      : `<div class="agent-avatar">${escapeHtml(a.name.charAt(0))}</div>`;
    return `
    <div class="agent-item${a.id === currentAgentId ? ' active' : ''}" data-agent-id="${a.id}">
      ${avatarHtml}
      <div class="agent-info">
        <div class="agent-name">${escapeHtml(a.name)}</div>
        <div class="agent-desc">${escapeHtml(a.description || '')}</div>
      </div>
    </div>`;
  }).join('');

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
  const modelSel = document.getElementById('model-select');
  if (agent?.type === 'team') {
    modelSel.style.display = 'none';
  } else {
    modelSel.style.display = '';
    if (agent) updateModelSelect(agent);
  }
  // プロセスステータスのポーリングを開始
  startProcessStatusPolling();
  // タスク詳細を閉じてタスク一覧を読み込む
  backToTaskList();
  await loadTasks();
  // レポート詳細を閉じてレポート一覧を読み込む
  document.getElementById('report-detail-pane').style.display = 'none';
  document.getElementById('report-list-view').style.display = 'flex';
  loadReports();
  // 注入プロンプトプレビューを表示
  showSystemPromptPreview(agentId);
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
  const agentName = agents.find(a => a.id === currentAgentId)?.name || currentAgentId;
  el.innerHTML = `<div class="process-debug-title">${escapeHtml(agentName)} のプロセス (${processes.length})</div>${items}`;
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
  const agent = agents.find(a => a.id === currentAgentId);
  const url = agent?.type === 'team'
    ? `${API}/teams/${currentAgentId}/sessions`
    : `${API}/agents/${currentAgentId}/sessions`;
  const resp = await fetch(url);
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
    const date = formatDate(s.updated_at);
    const isInferring = inferringSessions.has(s.session_id);
    const statusHtml = isInferring
      ? '<div class="conv-status"><div class="spinner"></div><span class="label">応答待ち</span></div>'
      : '';
    const titleHtml = s.title
      ? `<div class="conv-title">${escapeHtml(s.title)}</div>`
      : '';
    const initiatedHtml = s.initiated_by
      ? `<div class="conv-initiated-by">via ${escapeHtml(s.initiated_by)}</div>`
      : '';
    return `
      <div class="conversation-item${s.session_id === currentSessionId ? ' active' : ''}" data-session-id="${s.session_id}">
        <div class="conv-header">
          <span class="conv-date">${date} 更新</span>
          <span class="conv-header-right">
            ${s.initiated_by ? `<img class="conv-badge-thumb" src="${API}/agents/${escapeHtml(s.initiated_by)}/thumbnail" alt="">` : ''}
            <span class="conv-count">(${s.message_count})</span>
          </span>
        </div>
        ${titleHtml}
        ${initiatedHtml}
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
  if (agent?.type === 'team') {
    // チームエージェントはモデル選択非表示
    sel.style.display = 'none';
  } else {
    sel.style.display = '';
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
        sel.value = agent.model_tier || 'quick';
      }
    }
    applyModelSelectStyle(sel);
  }
  showSystemPromptPreview(currentAgentId);
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

  const agentForHistory = agents.find(a => a.id === currentAgentId);
  const isTeam = agentForHistory?.type === 'team';
  const histUrl = isTeam
    ? `${API}/teams/${currentAgentId}/sessions/${sessionId}`
    : `${API}/agents/${currentAgentId}/sessions/${sessionId}`;
  const resp = await fetch(histUrl);
  const messages = await resp.json();
  renderMessages(messages, sessionId, isTeam);

  const date = messages.length > 0
    ? formatDate(messages[0].timestamp)
    : '';
  document.getElementById('chat-title').textContent = currentSessionTitle || (date ? `${date} の会話` : '');

  // ファイル・タスクリンク表示（チームセッションは非対応）
  const linkedFileEl = document.getElementById('chat-linked-file');
  linkedFileEl.style.display = 'none';
  if (!isTeam) {
    fetch(`${API}/agents/${currentAgentId}/sessions/${sessionId}/meta`)
      .then(r => r.json())
      .then(meta => {
        if (meta.linked_task) {
          const title = meta.linked_task_title || meta.linked_task;
          linkedFileEl.innerHTML = `📋 ${escapeHtml(title)}`;
          linkedFileEl.dataset.linkPath = meta.linked_task;
          linkedFileEl.dataset.linkType = 'task';
          linkedFileEl.style.display = 'block';
        } else if (meta.linked_file) {
          const fname = meta.linked_file.split('/').pop();
          linkedFileEl.innerHTML = `📎 ${escapeHtml(fname)}`;
          linkedFileEl.dataset.linkPath = meta.linked_file;
          linkedFileEl.dataset.linkType = 'file';
          linkedFileEl.style.display = 'block';
        }
      })
      .catch(() => {});
  }
}

const MSG_PAGE = 100;

function renderMessages(messages, sessionId, isTeam = false) {
  const key = sessionId || currentSessionId;
  const container = getSessionContainer(key);
  container.querySelectorAll('.thinking-indicator').forEach(el => el._stopTimer?.());
  container.innerHTML = '';
  if (sessionDomCache[key]) {
    sessionDomCache[key].stale = false;
    sessionDomCache[key].allMessages = messages;
    sessionDomCache[key].isTeam = isTeam;
  }
  const from = Math.max(0, messages.length - MSG_PAGE);
  if (from > 0) container.appendChild(_makeLoadMoreBtn(key, from, container));
  _appendMsgRange(container, messages, from, messages.length, isTeam);
  updateAssistantTimestamps(container);
  container.scrollTop = container.scrollHeight;
}

function _makeLoadMoreBtn(key, upTo, container) {
  const btn = document.createElement('button');
  btn.className = 'load-more-btn';
  btn.textContent = `過去 ${upTo} 件を読み込む`;
  btn.onclick = () => {
    const msgs = sessionDomCache[key]?.allMessages;
    if (!msgs) return;
    const isTeam = sessionDomCache[key]?.isTeam || false;
    const newFrom = Math.max(0, upTo - MSG_PAGE);
    btn.remove();
    const frag = document.createDocumentFragment();
    if (newFrom > 0) frag.appendChild(_makeLoadMoreBtn(key, newFrom, container));
    _appendMsgRange(frag, msgs, newFrom, upTo, isTeam);
    container.insertBefore(frag, container.firstChild);
    updateAssistantTimestamps(container);
  };
  return btn;
}

function _appendMsgRange(parent, messages, from, to, isTeam = false) {
  const agent = agents.find(a => a.id === currentAgentId);
  const avatarHtml = agent?.thumbnail_url
    ? `<img src="${agent.thumbnail_url}" class="msg-avatar" alt="">`
    : `<div class="msg-avatar-letter">${escapeHtml((agent?.name || '?').charAt(0))}</div>`;

  for (let i = from; i < to; i++) {
    const m = messages[i];
    const content = (m.content || '').trim();
    if (!content && (!m.tool_uses || m.tool_uses.length === 0)) continue;

    if (m.tool_uses?.length > 0) {
      m.tool_uses.forEach(tu => {
        const notice = document.createElement('div');
        notice.className = 'tool-use-notice';
        notice.innerHTML = `<span class="icon">&#9881;</span> ${escapeHtml(describeToolUse(tu))}`;
        parent.appendChild(notice);
      });
    }
    if (!content) continue;

    // チームセッションのエージェント発言
    if (isTeam && m.role === 'agent') {
      const agentName = m.agent_name || m.agent_id || 'エージェント';
      const bubbleContent = typeof marked !== 'undefined' ? marked.parse(content) : escapeHtml(content);
      const div = document.createElement('div');
      div.className = 'message assistant';
      div.innerHTML = `
        <div class="msg-avatar-col"><div class="msg-avatar-letter">${escapeHtml(agentName.charAt(0))}</div></div>
        <div class="msg-body">
          <div class="team-agent-name">${escapeHtml(agentName)}</div>
          <div class="message-bubble">${bubbleContent}</div>
        </div>`;
      parent.appendChild(div);
      continue;
    }

    const div = document.createElement('div');
    div.className = `message ${m.role}`;
    const time = formatDate(m.timestamp);
    if (m.role === 'assistant') {
      const bubbleContent = typeof marked !== 'undefined' ? marked.parse(content) : escapeHtml(content);
      div.innerHTML = `
        <div class="msg-avatar-col">${avatarHtml}</div>
        <div class="msg-body">
          <div class="message-bubble">${bubbleContent}</div>
          <div class="message-time">${time}</div>
        </div>`;
    } else {
      div.innerHTML = `
        <div class="message-bubble">${escapeHtml(content)}</div>
        <div class="message-time">${time}</div>`;
    }
    parent.appendChild(div);
  }
}

function updateAssistantTimestamps(container) {
  const items = Array.from(container.children);
  for (let i = 0; i < items.length; i++) {
    if (!items[i].classList.contains('assistant')) continue;
    let followed = false;
    for (let j = i + 1; j < items.length; j++) {
      if (items[j].classList.contains('assistant')) { followed = true; break; }
      if (!items[j].classList.contains('tool-use-notice')) break;
    }
    items[i].classList.toggle('hide-time', followed);
  }
}

const MODEL_LABELS = {
  claude: { deep: 'opus', quick: 'sonnet' },
  codex:  { deep: 'gpt-5', quick: 'gpt-5' },
};

function updateModelSelect(agent) {
  const sel = document.getElementById('model-select');
  const labels = MODEL_LABELS[agent.cli] || MODEL_LABELS.claude;
  sel.innerHTML = Object.entries(labels)
    .map(([tier, label]) => `<option value="${tier}">${label}</option>`)
    .join('');
  sel.value = agent.model_tier || 'quick';
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
  document.getElementById('chat-linked-file').style.display = 'none';
}

async function saveSessionTitle(title) {
  if (!currentAgentId || !currentSessionId) return;
  const agent = agents.find(a => a.id === currentAgentId);
  const titleUrl = agent?.type === 'team'
    ? `${API}/teams/${currentAgentId}/sessions/${currentSessionId}/title`
    : `${API}/agents/${currentAgentId}/sessions/${currentSessionId}/title`;
  await fetch(titleUrl, {
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
  showSystemPromptPreview(currentAgentId);
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
  showSystemPromptPreview(currentAgentId);
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

  const currentAgentObj = agents.find(a => a.id === sentAgentId);
  const isTeamAgent = currentAgentObj?.type === 'team';

  try {
    let chatUrl, body;
    if (isTeamAgent) {
      chatUrl = `${API}/teams/${sentAgentId}/chat`;
      body = { message, session_id: sentSessionId };
    } else {
      const modelTier = document.getElementById('model-select').value;
      chatUrl = `${API}/agents/${sentAgentId}/chat`;
      body = { message, session_id: sentSessionId, model_tier: modelTier };
      if (opts.task_id) body.task_id = opts.task_id;
      if (opts.task_mode) body.task_mode = opts.task_mode;
    }
    const resp = await fetch(chatUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: abort.signal,
    });

    // HTTPエラー（4xx/5xx）はSSEではなくJSONで返る
    if (!resp.ok) {
      let detail = `エラー (${resp.status})`;
      try {
        const err = await resp.json();
        if (err.detail) detail = err.detail;
      } catch (_) {}
      streamError = detail;
      throw new Error(detail);
    }

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
    // チームの場合: 現在発言中のエージェント名を追跡
    let currentTeamAgentName = null;

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
                if (isTeamAgent) {
                  // チーム: エージェントが切り替わったら新しいバブルを作成
                  const agentName = event.agent_name || event.agent_id || 'エージェント';
                  if (!bubbleEl || agentName !== currentTeamAgentName) {
                    currentTeamAgentName = agentName;
                    fullResponse = event.data;
                    const div = document.createElement('div');
                    div.className = 'message assistant';
                    div.innerHTML = `
                      <div class="msg-avatar-col"><div class="msg-avatar-letter">${escapeHtml(agentName.charAt(0))}</div></div>
                      <div class="msg-body">
                        <div class="team-agent-name">${escapeHtml(agentName)}</div>
                        <div class="message-bubble">${escapeHtml(fullResponse)}</div>
                      </div>`;
                    streamContainer.appendChild(div);
                    bubbleEl = div;
                  } else {
                    bubbleEl.querySelector('.message-bubble').textContent = fullResponse;
                  }
                } else {
                  if (!bubbleEl) {
                    bubbleEl = appendMessage('assistant', '');
                  }
                  bubbleEl.querySelector('.message-bubble').textContent = fullResponse;
                }
                // thinkingElを常に末尾に移動してスクロールアウトを防ぐ
                if (thinkingEl.parentNode) streamContainer.appendChild(thinkingEl);
              }
            } else if (event.type === 'routing') {
              // チームファシリテーターが次の発言者を選択
              if (isViewingThisSession() && isTeamAgent) {
                const agentName = event.agent_name || event.agent_id || 'エージェント';
                const timerEl = thinkingEl.querySelector('.thinking-timer');
                thinkingEl.innerHTML = `<div class="spinner"></div> ${escapeHtml(agentName)} が回答中 `;
                if (timerEl) thinkingEl.appendChild(timerEl);
                if (thinkingEl.parentNode) streamContainer.appendChild(thinkingEl);
                bubbleEl = null; // 次のチャンクで新バブル作成
                currentTeamAgentName = null;
                fullResponse = '';
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
                // ファイルリンクが pending なら保存
                if (pendingFileLinkPath && sentAgentId) {
                  const fp = pendingFileLinkPath;
                  pendingFileLinkPath = null;
                  fetch(`${API}/agents/${sentAgentId}/file-links`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_path: fp, session_id: newSessionId, title: fp.split('/').pop() }),
                  });
                }
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
      } else {
        showToast(streamError);
      }
      stopBtn.style.display = 'none';
      sendBtn.style.display = '';
      currentStreamAbort = null;
      await loadSessions();
      return;
    }

    // 完了 — Markdownレンダリング（チームは履歴再取得で表示）
    if (!isTeamAgent && bubbleEl && fullResponse && typeof marked !== 'undefined') {
      bubbleEl.querySelector('.message-bubble').innerHTML = marked.parse(fullResponse);
    }
    if (isTeamAgent && isViewingThisSession()) {
      // チームセッション: 完了後に履歴を再取得してマークダウンレンダリング
      await loadSessionHistory(sentSessionId, true);
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
  if (role === 'assistant') {
    const avatarHtml = agent?.thumbnail_url
      ? `<img src="${agent.thumbnail_url}" class="msg-avatar" alt="">`
      : `<div class="msg-avatar-letter">${escapeHtml((agent?.name || '?').charAt(0))}</div>`;
    div.innerHTML = `
      <div class="msg-avatar-col">${avatarHtml}</div>
      <div class="msg-body">
        <div class="message-bubble">${escapeHtml(content)}</div>
        <div class="message-time">${time}</div>
      </div>
    `;
  } else {
    div.innerHTML = `
      <div class="message-bubble">${escapeHtml(content)}</div>
      <div class="message-time">${time}</div>
    `;
  }
  container.appendChild(div);
  updateAssistantTimestamps(container);
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

function hideSystemPromptPreview() {
  const host = document.getElementById('system-prompt-preview-host');
  if (host) host.innerHTML = '';
}

async function showSystemPromptPreview(agentId) {
  if (!agentId) return;
  const host = document.getElementById('system-prompt-preview-host');
  if (!host) return;
  host.innerHTML = '';

  let data;
  try {
    const resp = await fetch(`${API}/agents/${agentId}/system-prompt`);
    if (!resp.ok) return;
    data = await resp.json();
  } catch (_) { return; }

  if (!data.content && !data.shared_instructions) return;

  const agent = agents.find(a => a.id === agentId);
  const mdFileName = (agent?.cli === 'codex') ? 'AGENTS.md' : 'CLAUDE.md';

  let sectionsHtml = '';
  if (data.content) {
    sectionsHtml += `<div class="spp-section">
      <div class="spp-section-title">${mdFileName}</div>
      <pre class="spp-content">${escapeHtml(data.content)}</pre>
    </div>`;
  }
  if (data.shared_instructions) {
    sectionsHtml += `<div class="spp-section">
      <div class="spp-section-title">共通指示 (shared_instructions.md)</div>
      <pre class="spp-content">${escapeHtml(data.shared_instructions)}</pre>
    </div>`;
  }

  const preview = document.createElement('div');
  preview.className = 'system-prompt-preview';
  preview.innerHTML = `
    <div class="spp-header" onclick="this.closest('.system-prompt-preview').classList.toggle('expanded')">
      <span class="spp-icon">&#9881;</span>
      <span class="spp-label">注入されるプロンプト</span>
      <span class="spp-chevron">&#9660;</span>
    </div>
    <div class="spp-body">${sectionsHtml}</div>`;

  host.appendChild(preview);
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
    document.querySelector('.layout').classList.remove('mobile-chat-active');
    settingsContent.classList.add('visible');
    chatPane.classList.add('hidden');
    settingsPane.classList.add('visible');
    loadSettingsData();
  }
}

// ============================================================
// ファイルブラウザ
// ============================================================

let fileBrowserPath = '';       // 現在表示中のディレクトリパス（ルートからの相対）
let fileBrowserSort = 'mtime';  // 'name' | 'mtime'
let fileBrowserCache = null;    // 最後にフェッチしたディレクトリデータ
let currentFilePath = null;      // 詳細ペインで表示中のファイルパス
let pendingFileLinkPath = null;  // 新規セッション確立後にリンクする予定のファイルパス

async function updateFileSessionBanner(filepath) {
  const banner = document.getElementById('file-session-banner');
  const link = document.getElementById('file-session-link');
  if (!filepath || !currentAgentId) { banner.style.display = 'none'; return; }
  try {
    const data = await fetch(`${API}/agents/${currentAgentId}/file-links?path=${encodeURIComponent(filepath)}`).then(r => r.json());
    if (!data.session_id) { banner.style.display = 'none'; return; }
    const sid = data.session_id;
    const session = sessions.find(s => s.session_id === sid);
    link.textContent = session?.title || sid.slice(0, 8) + '...';
    link.dataset.sessionId = sid;
    banner.style.display = 'flex';
  } catch (_) {
    banner.style.display = 'none';
  }
}

async function loadReports() {
  fileBrowserPath = '';
  fileBrowserCache = null;
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

  // ソートタブ更新
  document.getElementById('file-sort-name')?.classList.toggle('active', fileBrowserSort === 'name');
  document.getElementById('file-sort-mtime')?.classList.toggle('active', fileBrowserSort === 'mtime');

  try {
    const url = `${API}/agents/${currentAgentId}/reports?path=${encodeURIComponent(dirPath)}`;
    const data = await fetch(url).then(r => r.json());
    fileBrowserCache = data;
    renderFileDirEntries(data);

    renderFileDirEntries(data);
  } catch (e) {
    entryList.innerHTML = '<div style="padding:8px; color:var(--danger); font-size:13px;">読み込みエラー</div>';
  }
}

function renderFileDirEntries(data) {
  const entryList = document.getElementById('file-entry-list');
  let { dirs, files } = data;

  if (!dirs.length && !files.length) {
    entryList.innerHTML = '<div style="padding:8px; color:var(--text-muted); font-size:13px;">空のディレクトリ</div>';
    return;
  }

  const fmt = ts => formatDate(ts * 1000);

  const orderedAll = [
    ...dirs.map(d => ({ ...d, _type: 'dir' })),
    ...files.map(f => ({ ...f, _type: 'file' })),
  ];
  if (fileBrowserSort === 'mtime') {
    orderedAll.sort((a, b) => b.mtime - a.mtime);
  }

  let html = '';

  orderedAll.forEach(entry => {
    if (entry._type === 'dir') {
      html += `<div class="file-entry dir" data-path="${escapeHtml(entry.path)}">
        <div class="file-entry-row1"><span class="file-entry-icon">📁</span><span class="file-entry-name">${escapeHtml(entry.name)}</span></div>
        <div class="file-entry-meta">${fmt(entry.mtime)} 更新</div>
      </div>`;
    } else {
      const f = entry;
      const isMd = f.is_md;
      const isImage = f.is_image;
      const isJson = f.is_json;
      const isCode = f.is_code;
      const ext = f.name.split('.').pop().toLowerCase();
      const isHtml = ext === 'html';
      const cls = isMd ? 'file-md' : isImage ? 'file-img' : isHtml ? 'file-html' : isJson ? 'file-json' : isCode ? 'file-code' : 'file-other';
      const icon = isMd ? '📄' : isImage ? '🖼️' : isHtml ? '🌐' : isJson ? '{ }' : isCode ? '</>' : '🔒';
      const sizeStr = f.size < 1024 ? `${f.size}B` : `${(f.size / 1024).toFixed(1)}KB`;
      const meta = `${sizeStr} · ${fmt(f.mtime)} 更新`;
      const row2 = f.preview
        ? `<div class="file-entry-preview">${escapeHtml(f.preview)}</div><div class="file-entry-meta">${meta}</div>`
        : `<div class="file-entry-meta">${meta}</div>`;
      html += `<div class="file-entry ${cls}" data-path="${escapeHtml(f.path)}">
        <div class="file-entry-row1"><span class="file-entry-icon">${icon}</span><span class="file-entry-name">${escapeHtml(f.name)}</span></div>
        ${row2}
      </div>`;
    }
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
  entryList.querySelectorAll('.file-entry.file-json').forEach(el => {
    el.addEventListener('click', () => openJsonFile(el.dataset.path));
  });
  entryList.querySelectorAll('.file-entry.file-code').forEach(el => {
    el.addEventListener('click', () => openCodeFile(el.dataset.path));
  });
  entryList.querySelectorAll('.file-entry.file-html').forEach(el => {
    el.addEventListener('click', () => {
      const pathParts = el.dataset.path.split('/');
      const encoded = pathParts.map(encodeURIComponent).join('/');
      window.open(`${API}/agents/${currentAgentId}/reports/${encoded}`, '_blank');
    });
  });
}

async function openReport(filepath) {
  currentFilePath = filepath;
  updateFileSessionBanner(filepath);
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
  currentFilePath = filepath;
  updateFileSessionBanner(filepath);
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

function highlightJson(text) {
  try {
    const parsed = JSON.parse(text);
    text = JSON.stringify(parsed, null, 2);
  } catch (_) {}
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/("(\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, match => {
      if (/^"/.test(match)) {
        return /:$/.test(match)
          ? `<span class="json-key">${match}</span>`
          : `<span class="json-str">${match}</span>`;
      }
      if (/true|false/.test(match)) return `<span class="json-bool">${match}</span>`;
      if (/null/.test(match)) return `<span class="json-null">${match}</span>`;
      return `<span class="json-num">${match}</span>`;
    });
}

async function openJsonFile(filepath) {
  currentFilePath = filepath;
  updateFileSessionBanner(filepath);
  const detailPane = document.getElementById('report-detail-pane');
  const listView = document.getElementById('report-list-view');
  const titleEl = document.getElementById('report-detail-title');
  const bodyEl = document.getElementById('report-detail-body');

  titleEl.textContent = filepath.split('/').pop();
  bodyEl.innerHTML = '<div style="color:var(--text-muted);">読み込み中...</div>';
  listView.style.display = 'none';
  detailPane.style.display = 'flex';

  try {
    const pathParts = filepath.split('/');
    const encoded = pathParts.map(encodeURIComponent).join('/');
    const text = await fetch(`${API}/agents/${currentAgentId}/reports/${encoded}`).then(r => r.text());
    bodyEl.innerHTML = `<pre class="json-viewer">${highlightJson(text)}</pre>`;
  } catch (e) {
    bodyEl.innerHTML = '<div style="color:var(--danger);">読み込みエラー</div>';
  }
}

async function openCodeFile(filepath) {
  currentFilePath = filepath;
  updateFileSessionBanner(filepath);
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
    if (typeof hljs !== 'undefined') {
      const ext = name.split('.').pop().toLowerCase();
      const lang = { py: 'python', js: 'javascript', ts: 'typescript', jsx: 'javascript',
        tsx: 'typescript', sh: 'bash', bash: 'bash', yml: 'yaml', yaml: 'yaml',
        toml: 'ini', rs: 'rust', go: 'go', rb: 'ruby', php: 'php',
        cpp: 'cpp', c: 'c', h: 'c', java: 'java', css: 'css', txt: 'plaintext',
      }[ext] || 'plaintext';
      const highlighted = hljs.highlight(text, { language: lang, ignoreIllegals: true }).value;
      bodyEl.innerHTML = `<pre class="hljs" style="font-size:12px; line-height:1.6; border-radius:6px; padding:14px; overflow-x:auto;">${highlighted}</pre>`;
    } else {
      const escaped = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      bodyEl.innerHTML = `<pre style="font-size:12px; line-height:1.6;">${escaped}</pre>`;
    }
  } catch (e) {
    bodyEl.innerHTML = '<div style="color:var(--danger);">読み込みエラー</div>';
  }
}

async function openFileByPath(filepath) {
  const ext = filepath.split('.').pop().toLowerCase();
  if (ext === 'md') return openReport(filepath);
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'].includes(ext)) return openImageFile(filepath);
  if (ext === 'json') return openJsonFile(filepath);
  return openCodeFile(filepath);
}

function initReports() {
  // チャットヘッダーのファイル・タスクリンク
  document.getElementById('chat-linked-file').addEventListener('click', async () => {
    const el = document.getElementById('chat-linked-file');
    const lp = el.dataset.linkPath;
    if (!lp) return;
    if (el.dataset.linkType === 'task') {
      switchTab('tasks');
      selectTask(lp);
    } else {
      switchTab('reports');
      const parts = lp.split('/');
      const dir = parts.length > 1 ? parts.slice(0, -1).join('/') : '';
      await renderFileDir(dir);
      await openFileByPath(lp);
    }
  });

  document.getElementById('report-back-btn').addEventListener('click', () => {
    document.getElementById('report-detail-pane').style.display = 'none';
    document.getElementById('report-list-view').style.display = 'flex';
    document.getElementById('file-session-banner').style.display = 'none';
    currentFilePath = null;
  });

  // ファイルセッションリンク
  document.getElementById('file-session-link').addEventListener('click', async () => {
    const sid = document.getElementById('file-session-link').dataset.sessionId;
    if (!sid) return;
    await selectSession(sid);
    switchTab('chat');
  });

  // ファイル詳細メニュー
  const fileDetailMenuBtn = document.getElementById('file-detail-menu-btn');
  const fileDetailMenu = document.getElementById('file-detail-menu');
  fileDetailMenuBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    fileDetailMenu.classList.toggle('open');
  });
  document.addEventListener('click', () => fileDetailMenu.classList.remove('open'));

  // ファイルについて話す
  document.getElementById('btn-file-talk').addEventListener('click', async () => {
    fileDetailMenu.classList.remove('open');
    if (!currentAgentId || !currentFilePath) return;
    // 既存リンクを確認
    const linkData = await fetch(`${API}/agents/${currentAgentId}/file-links?path=${encodeURIComponent(currentFilePath)}`).then(r => r.json());
    if (linkData.session_id) {
      // 既存セッションを開く
      await loadSessions();
      await selectSession(linkData.session_id);
    } else {
      // 新規チャット起動。セッションIDは最初のメッセージ送信後に確定する
      pendingFileLinkPath = currentFilePath;
      currentSessionId = null;
      clearChat();
      activateSessionContainer(null);
      showSystemPromptPreview(currentAgentId);
      document.querySelectorAll('.conversation-item').forEach(el => el.classList.remove('active'));
      switchTab('chat');
    }
  });

  document.querySelectorAll('.file-sort-tab').forEach(el => {
    el.addEventListener('click', () => {
      fileBrowserSort = el.dataset.sort;
      document.querySelectorAll('.file-sort-tab').forEach(t => t.classList.toggle('active', t.dataset.sort === fileBrowserSort));
      if (fileBrowserCache) renderFileDirEntries(fileBrowserCache);
    });
  });
}

// ============================================================
// 設定
// ============================================================

const MODEL_OPTIONS = {
  claude: [
    { value: 'quick', label: 'sonnet（標準）' },
    { value: 'deep',  label: 'opus（高精度）' },
  ],
  codex: [
    { value: 'quick', label: 'gpt-5' },
    { value: 'deep',  label: 'gpt-5' },
  ],
};

function fillModelOptions(cliValue, selectEl, currentTier) {
  const opts = MODEL_OPTIONS[cliValue] || MODEL_OPTIONS.claude;
  selectEl.innerHTML = opts.map(o =>
    `<option value="${o.value}"${o.value === currentTier ? ' selected' : ''}>${o.label}</option>`
  ).join('');
}

async function loadSettingsData() {
  if (!currentAgentId) return;

  const resp = await fetch(`${API}/agents/${currentAgentId}`);
  const agent = await resp.json();

  document.querySelector('[data-field="name"]').value = agent.name || '';
  document.querySelector('[data-field="description"]').value = agent.description || '';
  document.querySelector('[data-field="cli"]').value = agent.cli || 'claude';
  const modelSel = document.querySelector('[data-field="model_tier"]');
  fillModelOptions(agent.cli || 'claude', modelSel, agent.model_tier || 'quick');
  document.querySelector('[data-field="path"]').value = agent.path || '';
  const mdFileName = agent.cli === 'codex' ? 'AGENTS.md' : 'CLAUDE.md';
  document.getElementById('settings-pane-header').textContent = `${agent.name} — AI設定 (${mdFileName})`;
  document.getElementById('open-claude-md-btn').textContent = `AI設定 (${mdFileName}) を編集`;
  const spLabel = document.getElementById('system-prompt-label');
  if (spLabel) spLabel.textContent = mdFileName;

  const promptResp = await fetch(`${API}/agents/${currentAgentId}/system-prompt`);
  const promptData = await promptResp.json();
  document.querySelector('[data-field="system-prompt"]').value = promptData.content || '';

  // サムネイルプレビュー更新
  const preview = document.getElementById('settings-avatar-preview');
  const removeBtn = document.getElementById('thumbnail-remove-btn');
  if (agent.thumbnail_url) {
    preview.innerHTML = `<img src="${agent.thumbnail_url}" alt="">`;
    removeBtn.style.display = '';
  } else {
    preview.innerHTML = `<div class="agent-avatar settings-avatar-large">${escapeHtml(agent.name.charAt(0))}</div>`;
    removeBtn.style.display = 'none';
  }

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
    const dateStr = formatDate(log.timestamp);
    const logAgent = agents.find(a => a.id === log.agent_id);
    const agentAvatarHtml = logAgent?.thumbnail_url
      ? `<img src="${logAgent.thumbnail_url}" class="slog-avatar" alt="">`
      : `<div class="slog-avatar slog-avatar-letter">${escapeHtml((logAgent?.name || '?').charAt(0))}</div>`;
    const stepsHtml = (log.completed_steps && log.completed_steps.length > 0)
      ? log.completed_steps.map(s => `<div class="slog-step">✓ ${escapeHtml(s)}</div>`).join('')
      : `<span class="slog-unchanged">${escapeHtml(log.current_step || '—')} の作業中</span>`;
    const errHtml = log.error ? `<div class="slog-error">${escapeHtml(log.error)}</div>` : '';
    const sessionTitle = log.session_id
      ? (sessions.find(s => s.session_id === log.session_id)?.title || '作業セッション')
      : null;
    const sessionLink = log.session_id
      ? `<a class="slog-link" data-session-id="${log.session_id}" title="${log.session_id}">作業セッション: ${escapeHtml(sessionTitle)}</a>`
      : '—';
    const slogProgress = progressBarHtml(log.checked_after || 0, log.total_after || 0);
    return `<div class="scheduler-log-entry${log.error ? ' slog-has-error' : ''}">
      <div class="slog-header">
        ${agentAvatarHtml}
        <span class="slog-time">${dateStr}</span>
        <a class="slog-link slog-title" data-task-id="${log.task_id}" data-agent-id="${log.agent_id}">${escapeHtml(log.task_title || log.task_id)}</a>
      </div>
      ${slogProgress}
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
      const agentId = taskLink.dataset.agentId;
      if (agentId && agentId !== currentAgentId) await selectAgent(agentId);
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
    if (window.innerWidth <= 600) {
      switchTab('settings');
    } else {
      switchTab('chat');
    }
  });

  document.getElementById('open-claude-md-btn').addEventListener('click', () => {
    document.querySelector('.layout').classList.add('mobile-chat-active');
  });

  // CLI 変更時にモデル選択肢を切り替える
  document.querySelector('[data-field="cli"]').addEventListener('change', (e) => {
    const modelSel = document.querySelector('[data-field="model_tier"]');
    fillModelOptions(e.target.value, modelSel, modelSel.value);
  });

  // 設定保存（中央ペイン）
  document.getElementById('settings-save-btn').addEventListener('click', async () => {
    await fetch(`${API}/agents/${currentAgentId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: document.querySelector('[data-field="name"]').value,
        description: document.querySelector('[data-field="description"]').value,
        cli: document.querySelector('[data-field="cli"]').value,
        model_tier: document.querySelector('[data-field="model_tier"]').value,
      }),
    });
    await loadAgents({ autoSelect: false });
  });

  // サムネイルアップロード
  const thumbnailInput = document.getElementById('thumbnail-input');
  document.getElementById('settings-avatar-preview').addEventListener('click', () => thumbnailInput.click());
  thumbnailInput.addEventListener('change', async () => {
    const file = thumbnailInput.files[0];
    if (!file || !currentAgentId) return;
    const form = new FormData();
    form.append('file', file);
    const resp = await fetch(`${API}/agents/${currentAgentId}/thumbnail`, { method: 'POST', body: form });
    thumbnailInput.value = '';
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showToast(err.detail || 'アップロードに失敗しました');
      return;
    }
    await loadAgents({ autoSelect: false });
    loadSettingsData();
  });
  document.getElementById('thumbnail-remove-btn').addEventListener('click', async () => {
    if (!currentAgentId) return;
    await fetch(`${API}/agents/${currentAgentId}/thumbnail`, { method: 'DELETE' });
    await loadAgents({ autoSelect: false });
    loadSettingsData();
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
    showSystemPromptPreview(currentAgentId);
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
    if (!resp.ok) {
      tasksCache = {};
      taskExecutionOrder = [];
      renderTaskList();
      return;
    }
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
    tasksCache = {};
    taskExecutionOrder = [];
    renderTaskList();
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


function progressBarHtml(checked, total) {
  if (!total) return '';
  const pct = Math.round(checked / total * 100);
  let segmentStyle = '';
  if (total > 1) {
    const stops = Array.from({ length: total - 1 }, (_, i) => {
      const p = Math.round((i + 1) / total * 100);
      return `transparent ${p}%, var(--bg-primary) ${p}%, var(--bg-primary) calc(${p}% + 3px), transparent calc(${p}% + 3px)`;
    }).join(', ');
    segmentStyle = `style="background-image: linear-gradient(to right, ${stops})"`;
  }
  return `<div class="task-progress-wrap">
    <div class="task-progress-bar">
      <div class="task-progress-fill" style="width:${pct}%"></div>
      <div class="task-progress-segments" ${segmentStyle}></div>
    </div>
    <span class="task-progress-label">${checked} / ${total}</span>
  </div>`;
}

function taskProgressHtml(task) {
  const body = task.body || '';
  const total = (body.match(/- \[[ x]\]/g) || []).length;
  const checked = (body.match(/- \[x\]/gi) || []).length;
  return progressBarHtml(checked, total);
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
      ? '<span class="doing-indicator"><span class="dot"></span>作業中</span>'
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
        ${taskProgressHtml(task)}
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
        ${taskProgressHtml(latest)}
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
      ? formatDate(task.approved_at)
      : '';
    approvalHtml = `<div class="approval-status-badge approved">✓ 承認済み${dt ? ' (' + dt + ')' : ''}</div>`;
  }

  const phaseLabel = { draft: '未着手', doing: '作業中', done: '完了' }[task.phase] ?? task.phase;
  const phaseBadge = task.phase === 'doing'
    ? '<span class="doing-indicator"><span class="dot"></span>作業中</span>'
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
  const created = formatDate(task.created);
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

const _DOW = ['日','月','火','水','木','金','土'];
function formatDate(input) {
  const d = new Date(input);
  const now = new Date();
  const time = d.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const dDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const diffDays = Math.round((today - dDay) / 86400000);
  if (diffDays === 0) return `今日 ${time}`;
  if (diffDays === 1) return `昨日 ${time}`;
  const year = d.getFullYear() !== now.getFullYear() ? `${d.getFullYear()}/` : '';
  const date = `${d.getMonth() + 1}/${d.getDate()}${_DOW[d.getDay()]}`;
  return `${year}${date} ${time}`;
}

function formatTaskDate(iso) {
  return formatDate(iso);
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
    if (!confirm(`自律作業を${next}にしますか？`)) return;
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
  const nextRunEl = document.getElementById('scheduler-next-run');

  if (data.enabled) {
    btn.classList.add('on');
    indicator.classList.remove('off');
    indicator.classList.add('on');
    label.textContent = 'ON';
    if (data.next_run && nextRunEl) {
      const d = new Date(data.next_run);
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      nextRunEl.textContent = `${hh}:${mm}`;
    }
  } else {
    btn.classList.remove('on');
    indicator.classList.remove('on');
    indicator.classList.add('off');
    label.textContent = 'OFF';
    if (nextRunEl) nextRunEl.textContent = '';
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

  const typeSel = document.getElementById('add-agent-type');
  const cliSel = document.getElementById('add-agent-cli');
  const modelSel = document.getElementById('add-agent-model-tier');

  const pathSection = document.getElementById('add-agent-path-section');
  const cliSection = document.getElementById('add-agent-cli-section');
  const modelSection = document.getElementById('add-agent-model-section');
  const membersSection = document.getElementById('add-agent-members-section');
  const membersList = document.getElementById('add-agent-members-list');

  cliSel.addEventListener('change', () => {
    fillModelOptions(cliSel.value, modelSel, modelSel.value);
  });

  function applyTypeUI(type) {
    const isTeam = type === 'team';
    pathSection.style.display = isTeam ? 'none' : '';
    cliSection.style.display = isTeam ? 'none' : '';
    modelSection.style.display = isTeam ? 'none' : '';
    membersSection.style.display = isTeam ? '' : 'none';
    if (isTeam) renderMemberCheckboxes();
  }

  typeSel.addEventListener('change', () => applyTypeUI(typeSel.value));

  function renderMemberCheckboxes() {
    // 通常エージェントのみチェックボックスに表示
    const normalAgents = agents.filter(a => a.type !== 'team');
    membersList.innerHTML = normalAgents.map(a => `
      <label style="display:flex; align-items:center; gap:8px; cursor:pointer; padding:4px 0;">
        <input type="checkbox" class="member-checkbox" value="${a.id}">
        <span>${escapeHtml(a.name)}</span>
        <span style="color:var(--text-muted); font-size:11px;">${escapeHtml(a.description || '')}</span>
      </label>
    `).join('');
  }

  function openForm() {
    document.getElementById('add-agent-name').value = '';
    document.getElementById('add-agent-path').value = '';
    document.getElementById('add-agent-description').value = '';
    typeSel.value = 'agent';
    cliSel.value = 'claude';
    fillModelOptions('claude', modelSel, 'quick');
    applyTypeUI('agent');
    pane.style.display = 'flex';
  }

  function closeForm() {
    pane.style.display = 'none';
  }

  openBtn.addEventListener('click', openForm);
  cancelBtn.addEventListener('click', closeForm);
  cancelBtn2.addEventListener('click', closeForm);

  submitBtn.addEventListener('click', async () => {
    const type = typeSel.value;
    const name = document.getElementById('add-agent-name').value.trim();
    const description = document.getElementById('add-agent-description').value.trim();

    if (!name) {
      showToast('名前は必須です');
      return;
    }

    try {
      let resp;
      if (type === 'team') {
        const checked = [...membersList.querySelectorAll('.member-checkbox:checked')];
        const members = checked.map(cb => cb.value);
        if (members.length === 0) {
          showToast('メンバーを1人以上選択してください');
          return;
        }
        resp = await fetch(`${API}/agents/teams`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, description, members }),
        });
      } else {
        const path = document.getElementById('add-agent-path').value.trim();
        const cli = document.getElementById('add-agent-cli').value;
        const model_tier = document.getElementById('add-agent-model-tier').value;
        if (!path) {
          showToast('名前とプロジェクトパスは必須です');
          return;
        }
        resp = await fetch(`${API}/agents`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, path, description, cli, model_tier }),
        });
      }

      if (!resp.ok) {
        const err = await resp.json();
        showToast(err.detail || 'エラーが発生しました');
        return;
      }
      const newAgent = await resp.json();
      closeForm();
      await loadAgents({ autoSelect: false });
      await selectAgent(newAgent.id);
    } catch (e) {
      showToast('通信エラーが発生しました');
    }
  });
}
