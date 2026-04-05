/**
 * ポーリング状態遷移のテスト
 *
 * 実行: node --test src/tests/test_poll_logic.mjs
 *
 * 構成:
 *   正常系 — 現在のロジックが正しく動作するケース（全て通る）
 *   不具合検出 — 現在のロジックで不具合が起きるケース（落ちる = 不具合の証明）
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { decidePollActions, resetForPolling } from '../server/static/poll_logic.mjs';

// ============================================================
// ヘルパー
// ============================================================

function makeState(overrides = {}) {
  return {
    startupId: 'server-1',
    inferring: [],
    pendingEnd: [],
    activeStreams: [],
    dirMtime: 100,
    watchingMtime: 100,
    currentSessionId: 'sess-001',
    ...overrides,
  };
}

function makeResponse(overrides = {}) {
  return {
    startup_id: 'server-1',
    inferring: [],
    dir_mtime: 100,
    watching_mtime: 100,
    ...overrides,
  };
}

function hasAction(actions, type) {
  return actions.some(a => a.type === type);
}

function getActions(actions, type) {
  return actions.filter(a => a.type === type);
}

// ============================================================
// 正常系: サーバー再起動検知
// ============================================================

describe('サーバー再起動検知', () => {
  it('初回ポーリングではstartup_idを記録するだけでリロードしない', () => {
    const state = makeState({ startupId: null });
    const response = makeResponse({ startup_id: 'server-1' });
    const { nextState, actions } = decidePollActions(state, response);

    assert.equal(nextState.startupId, 'server-1');
    assert.ok(!hasAction(actions, 'server-restarted'));
  });

  it('startup_id不変なら何も発火しない', () => {
    const state = makeState({ startupId: 'server-1' });
    const response = makeResponse({ startup_id: 'server-1' });
    const { actions } = decidePollActions(state, response);

    assert.ok(!hasAction(actions, 'server-restarted'));
  });

  it('startup_id変化でserver-restartedが発火する', () => {
    const state = makeState({ startupId: 'server-1' });
    const response = makeResponse({ startup_id: 'server-2' });
    const { nextState, actions } = decidePollActions(state, response);

    assert.ok(hasAction(actions, 'server-restarted'));
    assert.equal(nextState.startupId, 'server-2');
  });
});

// ============================================================
// 正常系: 推論状態変化
// ============================================================

describe('推論状態変化', () => {
  it('推論開始を検知する', () => {
    const state = makeState({ inferring: [] });
    const response = makeResponse({ inferring: ['sess-001'] });
    const { nextState, actions } = decidePollActions(state, response);

    assert.ok(hasAction(actions, 'inferring-started'));
    assert.deepEqual(nextState.inferring, ['sess-001']);
  });

  it('推論終了を検知する（2ポーリングで確定）', () => {
    // ポーリング1: 猶予期間に入る
    const state1 = makeState({ inferring: ['sess-001'] });
    const resp1 = makeResponse({ inferring: [] });
    const { nextState: state2, actions: actions1 } = decidePollActions(state1, resp1);
    assert.ok(!hasAction(actions1, 'inferring-ended'), '猶予中は発火しない');

    // ポーリング2: 確定
    const resp2 = makeResponse({ inferring: [] });
    const { actions: actions2 } = decidePollActions(state2, resp2);
    assert.ok(hasAction(actions2, 'inferring-ended'));
  });

  it('推論終了 + 表示中セッション → reload-historyが発火する（2ポーリングで確定）', () => {
    const state1 = makeState({ inferring: ['sess-001'], currentSessionId: 'sess-001' });
    const resp1 = makeResponse({ inferring: [] });
    const { nextState: state2 } = decidePollActions(state1, resp1);

    const resp2 = makeResponse({ inferring: [] });
    const { actions } = decidePollActions(state2, resp2);

    const reloads = getActions(actions, 'reload-history');
    assert.ok(reloads.some(a => a.sessionId === 'sess-001'));
  });

  it('推論終了 + 別セッション表示中 → reload-historyは発火しない', () => {
    const state1 = makeState({ inferring: ['sess-002'], currentSessionId: 'sess-001' });
    const resp1 = makeResponse({ inferring: [] });
    const { nextState: state2 } = decidePollActions(state1, resp1);

    const resp2 = makeResponse({ inferring: [] });
    const { actions } = decidePollActions(state2, resp2);

    const reloads = getActions(actions, 'reload-history');
    assert.ok(!reloads.some(a => a.sessionId === 'sess-001'));
  });

  it('変化がなければ何も発火しない', () => {
    const state = makeState({ inferring: ['sess-001'] });
    const response = makeResponse({ inferring: ['sess-001'] });
    const { actions } = decidePollActions(state, response);

    assert.ok(!hasAction(actions, 'inferring-started'));
    assert.ok(!hasAction(actions, 'inferring-ended'));
  });

  it('複数セッションの開始/終了を個別に検知する（2ポーリングで確定）', () => {
    // ポーリング1: sess-001 終了開始、sess-003 開始
    const state1 = makeState({ inferring: ['sess-001', 'sess-002'] });
    const resp1 = makeResponse({ inferring: ['sess-002', 'sess-003'] });
    const { nextState: state2, actions: actions1 } = decidePollActions(state1, resp1);

    // sess-003 は即座に開始
    assert.ok(getActions(actions1, 'inferring-started').some(a => a.sessionId === 'sess-003'));
    // sess-001 はまだ猶予中
    assert.ok(!hasAction(actions1, 'inferring-ended'));

    // ポーリング2: sess-001 が確定終了
    const resp2 = makeResponse({ inferring: ['sess-002', 'sess-003'] });
    const { actions: actions2 } = decidePollActions(state2, resp2);

    const ended = getActions(actions2, 'inferring-ended');
    assert.ok(ended.some(a => a.sessionId === 'sess-001'));
    assert.ok(!ended.some(a => a.sessionId === 'sess-002'));
  });
});

// ============================================================
// 正常系: dir_mtime / watching_mtime
// ============================================================

describe('dir_mtime変化', () => {
  it('初回は記録のみでリロードしない', () => {
    const state = makeState({ dirMtime: 0 });
    const response = makeResponse({ dir_mtime: 100 });
    const { nextState, actions } = decidePollActions(state, response);

    assert.ok(!hasAction(actions, 'reload-sessions'));
    assert.equal(nextState.dirMtime, 100);
  });

  it('変化でreload-sessionsが発火する', () => {
    const state = makeState({ dirMtime: 100 });
    const response = makeResponse({ dir_mtime: 200 });
    const { nextState, actions } = decidePollActions(state, response);

    assert.ok(hasAction(actions, 'reload-sessions'));
    assert.equal(nextState.dirMtime, 200);
  });

  it('不変なら発火しない', () => {
    const state = makeState({ dirMtime: 100 });
    const response = makeResponse({ dir_mtime: 100 });
    const { actions } = decidePollActions(state, response);

    assert.ok(!hasAction(actions, 'reload-sessions'));
  });
});

describe('watching_mtime変化', () => {
  it('初回は記録のみでリロードしない', () => {
    const state = makeState({ watchingMtime: 0 });
    const response = makeResponse({ watching_mtime: 100 });
    const { nextState, actions } = decidePollActions(state, response);

    assert.ok(!hasAction(actions, 'reload-history'));
    assert.equal(nextState.watchingMtime, 100);
  });

  it('変化でreload-historyが発火する', () => {
    const state = makeState({ watchingMtime: 100 });
    const response = makeResponse({ watching_mtime: 200 });
    const { actions } = decidePollActions(state, response);

    assert.ok(hasAction(actions, 'reload-history'));
  });

  it('SSEストリーム中はスキップする', () => {
    const state = makeState({
      inferring: [],
      activeStreams: ['sess-001'],
      currentSessionId: 'sess-001',
      watchingMtime: 100,
    });
    const response = makeResponse({
      inferring: ['sess-001'],
      watching_mtime: 200,
    });
    const { actions } = decidePollActions(state, response);

    // watching_mtime由来のreload-historyは出ない（SSEストリームが更新するため）
    const reloads = getActions(actions, 'reload-history');
    assert.equal(reloads.length, 0);
  });

  it('推論中でもSSEストリームがなければ更新する', () => {
    const state = makeState({
      inferring: ['sess-001'],
      activeStreams: [],  // SSEストリームなし（CLI経由の推論）
      currentSessionId: 'sess-001',
      watchingMtime: 100,
    });
    const response = makeResponse({
      inferring: ['sess-001'],
      watching_mtime: 200,
    });
    const { actions } = decidePollActions(state, response);

    assert.ok(hasAction(actions, 'reload-history'));
  });
});

// ============================================================
// 不具合検出
// ============================================================

describe('エージェント切り替え時の状態リセット', () => {
  it('dirMtime がリセットされ、初回ポーリングでリロードしない', () => {
    const stateAfterReset = resetForPolling();
    const response = makeResponse({ dir_mtime: 100 });
    const { actions } = decidePollActions(stateAfterReset, response);

    assert.ok(!hasAction(actions, 'reload-sessions'));
  });

  it('watchingMtime がリセットされ、初回ポーリングでリロードしない', () => {
    const stateAfterReset = resetForPolling();
    const response = makeResponse({ watching_mtime: 50 });
    const { actions } = decidePollActions(stateAfterReset, response);

    assert.ok(!hasAction(actions, 'reload-history'));
  });

  it('startupId がリセットされ、初回ポーリングでserver-restartedが発火しない', () => {
    const stateAfterReset = resetForPolling();
    const response = makeResponse({ startup_id: 'server-new' });
    const { actions } = decidePollActions(stateAfterReset, response);

    assert.ok(!hasAction(actions, 'server-restarted'));
  });
});

describe('推論中（SSEなし）の一覧/画面の一貫性', () => {
  it('CLI推論中にdir_mtime + watching_mtime変化 → 一覧もセッション画面も更新される', () => {
    const state = makeState({
      inferring: ['sess-001'],
      activeStreams: [],  // CLI推論（SSEなし）
      currentSessionId: 'sess-001',
      dirMtime: 100,
      watchingMtime: 100,
    });

    const response = makeResponse({
      inferring: ['sess-001'],
      dir_mtime: 200,
      watching_mtime: 200,
    });

    const { actions } = decidePollActions(state, response);

    assert.ok(hasAction(actions, 'reload-sessions'));
    assert.ok(hasAction(actions, 'reload-history'));
  });

  it('SSEストリーム中はセッション画面を更新しない', () => {
    const state = makeState({
      inferring: ['sess-001'],
      activeStreams: ['sess-001'],  // SSEストリーム中
      currentSessionId: 'sess-001',
      dirMtime: 100,
      watchingMtime: 100,
    });

    const response = makeResponse({
      inferring: ['sess-001'],
      dir_mtime: 200,
      watching_mtime: 200,
    });

    const { actions } = decidePollActions(state, response);

    assert.ok(hasAction(actions, 'reload-sessions'));
    assert.ok(!hasAction(actions, 'reload-history'));
  });
});

describe('推論完了時のreload-history重複排除', () => {
  it('推論終了 + watching_mtime変化が同サイクル → reload-historyは1回だけ', () => {
    // 猶予期間を経て確定終了するケース
    // ポーリング1: バックエンドが推論なしと報告 → pendingEnd に入る
    const state1 = makeState({
      inferring: ['sess-001'],
      currentSessionId: 'sess-001',
      watchingMtime: 100,
    });
    const response1 = makeResponse({ inferring: [], watching_mtime: 150 });
    const { nextState: state2 } = decidePollActions(state1, response1);

    // ポーリング2: まだ推論なし → 確定終了 + watching_mtime変化
    const response2 = makeResponse({ inferring: [], watching_mtime: 200 });
    const { actions } = decidePollActions(state2, response2);

    const reloads = getActions(actions, 'reload-history');
    assert.equal(reloads.length, 1, `reload-history が ${reloads.length} 回発火`);
  });
});

describe('猶予期間: 楽観更新とバックエンドのタイミング差', () => {
  it('楽観更新した直後、バックエンドが未検知でもinferring-endedは発火しない', () => {
    const state = makeState({
      inferring: ['sess-001'],
      currentSessionId: 'sess-001',
    });
    const response = makeResponse({ inferring: [] });
    const { nextState, actions } = decidePollActions(state, response);

    // 猶予期間中: inferring-ended は発火しない
    assert.ok(!hasAction(actions, 'inferring-ended'));
    // inferring にはまだ残っている
    assert.ok(nextState.inferring.includes('sess-001'));
    // pendingEnd に入っている
    assert.ok(nextState.pendingEnd.includes('sess-001'));
  });

  it('次のポーリングでバックエンドが検知 → 猶予解除、推論中を継続', () => {
    // ポーリング1: バックエンド未検知 → pendingEnd
    const state1 = makeState({ inferring: ['sess-001'] });
    const resp1 = makeResponse({ inferring: [] });
    const { nextState: state2 } = decidePollActions(state1, resp1);

    // ポーリング2: バックエンドが検知した
    const resp2 = makeResponse({ inferring: ['sess-001'] });
    const { nextState: state3, actions } = decidePollActions(state2, resp2);

    assert.ok(!hasAction(actions, 'inferring-ended'));
    assert.ok(!hasAction(actions, 'inferring-started'));  // 見かけ上ずっと推論中
    assert.ok(state3.inferring.includes('sess-001'));
    assert.equal(state3.pendingEnd.length, 0);
  });

  it('2ポーリング連続で未検知 → 確定終了', () => {
    // ポーリング1: バックエンド未検知 → pendingEnd
    const state1 = makeState({ inferring: ['sess-001'], currentSessionId: 'sess-001' });
    const resp1 = makeResponse({ inferring: [] });
    const { nextState: state2, actions: actions1 } = decidePollActions(state1, resp1);
    assert.ok(!hasAction(actions1, 'inferring-ended'));

    // ポーリング2: まだ未検知 → 確定終了
    const resp2 = makeResponse({ inferring: [] });
    const { nextState: state3, actions: actions2 } = decidePollActions(state2, resp2);
    assert.ok(hasAction(actions2, 'inferring-ended'));
    assert.ok(!state3.inferring.includes('sess-001'));
    assert.equal(state3.pendingEnd.length, 0);
  });
});

describe('猶予期間: サーバー再起動時', () => {
  it('サーバー再起動 + 新サーバーが推論を未検知 → inferring-ended は発火しない', () => {
    const state = makeState({
      inferring: ['sess-001'],
      currentSessionId: 'sess-001',
      startupId: 'server-1',
    });
    const response = makeResponse({
      startup_id: 'server-2',
      inferring: [],
    });
    const { nextState, actions } = decidePollActions(state, response);

    assert.ok(hasAction(actions, 'server-restarted'));
    assert.ok(!hasAction(actions, 'inferring-ended'));
    assert.ok(nextState.inferring.includes('sess-001'));
  });

  it('次のポーリングで新サーバーが検知 → 推論中を継続', () => {
    // ポーリング1: 再起動 → pendingEnd
    const state1 = makeState({ inferring: ['sess-001'], startupId: 'server-1' });
    const resp1 = makeResponse({ startup_id: 'server-2', inferring: [] });
    const { nextState: state2 } = decidePollActions(state1, resp1);

    // ポーリング2: 新サーバーが検知
    const resp2 = makeResponse({ startup_id: 'server-2', inferring: ['sess-001'] });
    const { nextState: state3, actions } = decidePollActions(state2, resp2);

    assert.ok(!hasAction(actions, 'inferring-ended'));
    assert.ok(state3.inferring.includes('sess-001'));
    assert.equal(state3.pendingEnd.length, 0);
  });
});

describe('猶予期間: TCP検査の一時的な空白', () => {
  it('推論中 → 一瞬だけ未検知 → 次ポーリングで復活 — ちらつかない', () => {
    // ポーリング1: TCP空白
    const state1 = makeState({ inferring: ['sess-001'], currentSessionId: 'sess-001' });
    const resp1 = makeResponse({ inferring: [] });
    const { nextState: state2, actions: actions1 } = decidePollActions(state1, resp1);

    // ポーリング2: 復活
    const resp2 = makeResponse({ inferring: ['sess-001'] });
    const { actions: actions2 } = decidePollActions(state2, resp2);

    // どちらのポーリングでも inferring-ended / inferring-started は発火しない
    assert.ok(!hasAction(actions1, 'inferring-ended'));
    assert.ok(!hasAction(actions2, 'inferring-started'));
  });
});

// ============================================================
// 不変条件: サーバー再起動時は推論状態を信用しない
// ============================================================

describe('不変条件: サーバー再起動と推論状態', () => {
  it('server-restarted 時に inferring-ended が発火しない', () => {
    const failures = [];

    const inferringStates = [['sess-A'], ['sess-A', 'sess-B']];
    const responseInferring = [[], ['sess-A'], ['sess-B']];

    for (const prevInf of inferringStates) {
      for (const respInf of responseInferring) {
        const state = makeState({
          startupId: 'server-1',
          inferring: prevInf,
          currentSessionId: 'sess-A',
        });
        const response = makeResponse({
          startup_id: 'server-2',
          inferring: respInf,
        });
        const { actions } = decidePollActions(state, response);

        if (hasAction(actions, 'server-restarted') && hasAction(actions, 'inferring-ended')) {
          const ended = getActions(actions, 'inferring-ended').map(a => a.sessionId);
          failures.push(
            `prev=${JSON.stringify(prevInf)} resp=${JSON.stringify(respInf)} → inferring-ended: [${ended}]`
          );
        }
      }
    }

    assert.equal(failures.length, 0,
      `サーバー再起動時に inferring-ended が発火:\n${failures.join('\n')}`);
  });
});

// ============================================================
// 全組み合わせ不変条件テスト
//
// 全ての入力パターンに対して「常に成り立つべきルール」を検査する。
// 個別テストでは見逃すエッジケースを機械的に洗い出す。
// ============================================================

describe('不変条件: 全324パターン検査', () => {
  // 状態空間の定義
  const startupIdVariants  = ['same', 'changed', 'initial'];
  const inferringVariants  = ['start', 'end', 'unchanged', 'empty-to-empty'];
  const currentSidVariants = ['inferring', 'not-inferring', 'null'];
  const dirMtimeVariants   = ['changed', 'unchanged', 'initial'];
  const watchMtimeVariants = ['changed', 'unchanged', 'initial'];
  const pendingEndVariants = ['no-pending', 'has-pending'];

  // 各バリアントから具体的な state + response を構築する
  function buildCase(startupV, inferV, curSidV, dirV, watchV, pendingV) {
    const state = {
      startupId:        startupV === 'initial' ? null : 'server-1',
      inferring:        (inferV === 'end' || inferV === 'unchanged') ? ['sess-A'] : [],
      pendingEnd:       [],
      activeStreams:     [],
      dirMtime:         dirV === 'initial' ? 0 : 100,
      watchingMtime:    watchV === 'initial' ? 0 : 100,
      currentSessionId: curSidV === 'null' ? null : 'sess-A',
    };

    // currentSessionId が推論中かどうかを調整
    if (curSidV === 'inferring' && !state.inferring.includes('sess-A')) {
      state.inferring = [...state.inferring, 'sess-A'];
    }

    // pendingEnd: sess-A が猶予中（前回バックエンドが未検知だった）
    if (pendingV === 'has-pending' && state.inferring.includes('sess-A')) {
      state.pendingEnd = ['sess-A'];
    }

    const response = {
      startup_id: startupV === 'changed' ? 'server-2' : 'server-1',
      inferring:
        inferV === 'start'         ? ['sess-A'] :
        inferV === 'end'           ? [] :
        inferV === 'unchanged'     ? ['sess-A'] :
        /* empty-to-empty */         [],
      dir_mtime:     dirV === 'changed' ? 200 : (dirV === 'initial' ? 50 : 100),
      watching_mtime: watchV === 'changed' ? 200 : (watchV === 'initial' ? 50 : 100),
    };

    return { state, response };
  }

  // テストケースを全生成
  const allCases = [];
  for (const s of startupIdVariants) {
    for (const i of inferringVariants) {
      for (const c of currentSidVariants) {
        for (const d of dirMtimeVariants) {
          for (const w of watchMtimeVariants) {
            for (const p of pendingEndVariants) {
              allCases.push({ s, i, c, d, w, p });
            }
          }
        }
      }
    }
  }

  // ---- 不変条件1: nextState は必ず response の値を反映する ----
  it(`状態反映: nextState が response の最新値を持つ (${allCases.length}パターン)`, () => {
    const failures = [];
    for (const { s, i, c, d, w, p } of allCases) {
      const { state, response } = buildCase(s, i, c, d, w, p);
      const { nextState } = decidePollActions(state, response);

      if (nextState.startupId !== response.startup_id) {
        failures.push(`[s=${s},i=${i},c=${c},d=${d},w=${w},p=${p}] startupId: ${nextState.startupId} !== ${response.startup_id}`);
      }
      if (nextState.dirMtime !== response.dir_mtime) {
        failures.push(`[s=${s},i=${i},c=${c},d=${d},w=${w},p=${p}] dirMtime: ${nextState.dirMtime} !== ${response.dir_mtime}`);
      }
      if (nextState.watchingMtime !== response.watching_mtime) {
        failures.push(`[s=${s},i=${i},c=${c},d=${d},w=${w},p=${p}] watchingMtime: ${nextState.watchingMtime} !== ${response.watching_mtime}`);
      }
    }
    assert.equal(failures.length, 0, `状態反映の不整合:\n${failures.join('\n')}`);
  });

  // ---- 不変条件2: 冪等性 — 同じ入力で2回呼んでも同じ結果 ----
  it(`冪等性: 同じ入力で2回呼んでも結果が同じ (${allCases.length}パターン)`, () => {
    const failures = [];
    for (const { s, i, c, d, w, p } of allCases) {
      const { state, response } = buildCase(s, i, c, d, w, p);
      const r1 = decidePollActions(state, response);
      const r2 = decidePollActions(state, response);

      const a1 = JSON.stringify(r1.actions);
      const a2 = JSON.stringify(r2.actions);
      if (a1 !== a2) {
        failures.push(`[s=${s},i=${i},c=${c},d=${d},w=${w},p=${p}] actions が異なる`);
      }
    }
    assert.equal(failures.length, 0, `冪等性の違反:\n${failures.join('\n')}`);
  });

  // ---- 不変条件3: 何も変化していなければアクションは空 ----
  it('無変化: 全てが前回と同じならアクションが空である', () => {
    const state = makeState({
      startupId: 'server-1',
      inferring: ['sess-A'],
      dirMtime: 100,
      watchingMtime: 100,
      currentSessionId: 'sess-A',
    });
    const response = makeResponse({
      startup_id: 'server-1',
      inferring: ['sess-A'],
      dir_mtime: 100,
      watching_mtime: 100,
    });
    const { actions } = decidePollActions(state, response);

    assert.equal(actions.length, 0, `何も変化していないのにアクションが発火: ${JSON.stringify(actions)}`);
  });

  // ---- 不変条件4: reload-history は1サイクルで最大1回 ----
  it(`重複排除: reload-history は同一セッションに対して1回まで (${allCases.length}パターン)`, () => {
    const failures = [];
    for (const { s, i, c, d, w, p } of allCases) {
      const { state, response } = buildCase(s, i, c, d, w, p);
      const { actions } = decidePollActions(state, response);

      const reloads = getActions(actions, 'reload-history');
      const sidCounts = {};
      for (const r of reloads) {
        sidCounts[r.sessionId] = (sidCounts[r.sessionId] || 0) + 1;
      }
      for (const [sid, count] of Object.entries(sidCounts)) {
        if (count > 1) {
          failures.push(`[s=${s},i=${i},c=${c},d=${d},w=${w},p=${p}] reload-history(${sid}) が ${count} 回`);
        }
      }
    }
    assert.equal(failures.length, 0, `reload-history の重複:\n${failures.join('\n')}`);
  });

  // ---- 不変条件5: 初回(mtime=0)ではリロードしない ----
  it(`初回安全: mtime=0 のときはリロード系アクションを出さない (${allCases.length}パターン)`, () => {
    const failures = [];
    for (const { s, i, c, d, w, p } of allCases) {
      const { state, response } = buildCase(s, i, c, d, w, p);
      const { actions } = decidePollActions(state, response);

      if (d === 'initial' && state.dirMtime === 0 && hasAction(actions, 'reload-sessions')) {
        failures.push(`[s=${s},i=${i},c=${c},d=initial,w=${w}] dirMtime=0 なのに reload-sessions`);
      }
      // watching_mtime=0 の場合、watching_mtime由来の reload-history は出ないべき
      // ただし inferring-ended 由来の reload-history は OK
      if (w === 'initial' && state.watchingMtime === 0) {
        const watchReloads = actions.filter(a =>
          a.type === 'reload-history' &&
          // inferring-ended の直後にある reload-history は inferring 由来なので除外
          !actions.some(b => b.type === 'inferring-ended' && b.sessionId === a.sessionId)
        );
        if (watchReloads.length > 0) {
          failures.push(`[s=${s},i=${i},c=${c},d=${d},w=initial] watchingMtime=0 なのに watching由来の reload-history`);
        }
      }
    }
    assert.equal(failures.length, 0, `初回安全の違反:\n${failures.join('\n')}`);
  });

  // ---- 不変条件6: 猶予期間中は inferring-ended が発火しない ----
  it(`猶予期間: pendingEnd でないセッションの初回離脱では inferring-ended が出ない (${allCases.length}パターン)`, () => {
    const failures = [];
    for (const { s, i, c, d, w, p } of allCases) {
      const { state, response } = buildCase(s, i, c, d, w, p);
      const { actions } = decidePollActions(state, response);

      const endedSids = getActions(actions, 'inferring-ended').map(a => a.sessionId);
      for (const sid of endedSids) {
        // inferring-ended が出たセッションは、前回 pendingEnd に入っていたはず
        if (!(state.pendingEnd || []).includes(sid)) {
          failures.push(
            `[s=${s},i=${i},c=${c},d=${d},w=${w},p=${p}] ` +
            `${sid} が pendingEnd でないのに inferring-ended が発火`
          );
        }
      }
    }
    assert.equal(failures.length, 0, `猶予期間の違反:\n${failures.join('\n')}`);
  });

  // ---- 不変条件7: pendingEnd のセッションはバックエンド確認で猶予解除される ----
  it(`猶予解除: pendingEnd のセッションがバックエンドで検知されたら pendingEnd から消える (${allCases.length}パターン)`, () => {
    const failures = [];
    for (const { s, i, c, d, w, p } of allCases) {
      const { state, response } = buildCase(s, i, c, d, w, p);
      const { nextState } = decidePollActions(state, response);

      const backendInferring = new Set(response.inferring);
      for (const sid of (nextState.pendingEnd || [])) {
        if (backendInferring.has(sid)) {
          failures.push(
            `[s=${s},i=${i},c=${c},d=${d},w=${w},p=${p}] ` +
            `${sid} がバックエンドで検知されているのに pendingEnd に残っている`
          );
        }
      }
    }
    assert.equal(failures.length, 0, `猶予解除の違反:\n${failures.join('\n')}`);
  });

  // ---- 不変条件8: 一覧と画面の一貫性 ----
  // dir_mtime が変化してセッション一覧が更新されるなら、
  // 同時に watching_mtime も変化していれば、セッション画面も更新されるべき
  // （ただし activeStreams 中のセッションは SSE が更新するので除外）
  it(`一貫性: 一覧が更新されるなら画面も更新されるべき (${allCases.length}パターン)`, () => {
    const failures = [];
    for (const { s, i, c, d, w, p } of allCases) {
      const { state, response } = buildCase(s, i, c, d, w, p);
      const { actions } = decidePollActions(state, response);

      const hasReloadSessions = hasAction(actions, 'reload-sessions');
      const hasReloadHistory = hasAction(actions, 'reload-history');
      const watchingChanged = response.watching_mtime !== state.watchingMtime && state.watchingMtime !== 0;
      const isStreaming = (state.activeStreams || []).includes(state.currentSessionId);

      // SSEストリーム中でなく、一覧が更新され、watching_mtimeも変化しているのに、
      // 履歴が更新されない → 不整合
      if (hasReloadSessions && watchingChanged && !hasReloadHistory && !isStreaming && state.currentSessionId) {
        failures.push(
          `[s=${s},i=${i},c=${c},d=${d},w=${w},p=${p}] ` +
          `reload-sessions=YES, watching_mtime変化=YES, reload-history=NO → 一覧だけ更新される`
        );
      }
    }
    assert.equal(failures.length, 0, `一覧/画面の不整合:\n${failures.join('\n')}`);
  });
});
