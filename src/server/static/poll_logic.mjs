/**
 * ポーリング状態遷移の純粋関数。
 * app.js の pollProcessStatus / syncInferringIndicators から
 * 「判断する部分」だけを切り出したもの。
 *
 * 入力: 前回の状態 + ポーリングレスポンス
 * 出力: 次の状態 + 実行すべきアクションのリスト
 */

/**
 * @typedef {Object} PollState
 * @property {string|null} startupId
 * @property {string[]} inferring      - 推論中セッションID（確定 + 猶予中を含む）
 * @property {string[]} pendingEnd     - バックエンドが「推論なし」と1回報告したセッション（猶予中）
 * @property {string[]} activeStreams  - SSEストリーム中のセッション（sendMessageが管理）
 * @property {number} dirMtime
 * @property {number} watchingMtime
 * @property {string|null} currentSessionId
 */

/**
 * @typedef {Object} PollResponse
 * @property {string} startup_id
 * @property {string[]} inferring
 * @property {number} dir_mtime
 * @property {number} [watching_mtime]
 */

/**
 * ポーリング結果から「何をすべきか」を決定する。
 *
 * @param {PollState} state
 * @param {PollResponse} response
 * @returns {{ nextState: PollState, actions: Array<{type: string, [key: string]: any}> }}
 */
export function decidePollActions(state, response) {
  const actions = [];
  const backendInferring = new Set(response.inferring);
  const prevAll = new Set([...state.inferring]);
  const prevPending = new Set(state.pendingEnd || []);
  const activeStreams = new Set(state.activeStreams || []);

  const nextInferring = new Set();
  const nextPendingEnd = new Set();

  const nextState = {
    startupId: state.startupId,
    inferring: [],       // 後で設定
    pendingEnd: [],      // 後で設定
    activeStreams: [...activeStreams],
    dirMtime: state.dirMtime,
    watchingMtime: state.watchingMtime,
    currentSessionId: state.currentSessionId,
  };

  // ---- 1. サーバー再起動検知 ----
  if (response.startup_id) {
    if (state.startupId && state.startupId !== response.startup_id) {
      actions.push({ type: 'server-restarted' });
    }
    nextState.startupId = response.startup_id;
  }

  // ---- 2. 推論中セッション同期（猶予期間付き） ----
  const ended = [];
  const started = [];

  // 前回推論中だったセッションを処理
  for (const sid of prevAll) {
    if (backendInferring.has(sid)) {
      // バックエンドが推論中と確認 → 確定、猶予解除
      nextInferring.add(sid);
    } else if (prevPending.has(sid)) {
      // 猶予中 + まだ推論なし → 確定終了
      ended.push(sid);
    } else {
      // 初めて「推論なし」と報告された → 猶予期間に入る（まだ終了扱いしない）
      nextInferring.add(sid);
      nextPendingEnd.add(sid);
    }
  }

  // 新たに推論開始したセッション
  for (const sid of backendInferring) {
    if (!prevAll.has(sid)) {
      started.push(sid);
      nextInferring.add(sid);
    }
  }

  // アクション発行
  const reloadedSessions = new Set();

  for (const sid of ended) {
    actions.push({ type: 'inferring-ended', sessionId: sid });
    if (sid === state.currentSessionId && !reloadedSessions.has(sid)) {
      actions.push({ type: 'reload-history', sessionId: sid });
      reloadedSessions.add(sid);
    }
  }

  for (const sid of started) {
    actions.push({ type: 'inferring-started', sessionId: sid });
  }

  nextState.inferring = [...nextInferring];
  nextState.pendingEnd = [...nextPendingEnd];

  // ---- 3. dir_mtime 変化 → セッション一覧を再取得 ----
  if (response.dir_mtime && response.dir_mtime !== state.dirMtime) {
    if (state.dirMtime !== 0) {
      actions.push({ type: 'reload-sessions' });
    }
    nextState.dirMtime = response.dir_mtime;
  }

  // ---- 4. watching_mtime 変化 → 表示中セッションの履歴を再取得 ----
  // SSEストリーム中のみスキップ（推論中でもSSEがなければ更新する）
  if (response.watching_mtime && response.watching_mtime !== state.watchingMtime) {
    if (state.watchingMtime !== 0
        && !activeStreams.has(state.currentSessionId)
        && !reloadedSessions.has(state.currentSessionId)) {
      actions.push({ type: 'reload-history', sessionId: state.currentSessionId });
      reloadedSessions.add(state.currentSessionId);
    }
    nextState.watchingMtime = response.watching_mtime;
  }

  return { nextState, actions };
}

/**
 * エージェント切り替え時の状態リセット。
 * 全てのポーリング状態を初期化する。
 *
 * @returns {PollState}
 */
export function resetForPolling(overrides = {}) {
  return {
    startupId: null,
    inferring: [],
    pendingEnd: [],
    activeStreams: [],
    dirMtime: 0,
    watchingMtime: 0,
    currentSessionId: null,
    ...overrides,
  };
}
