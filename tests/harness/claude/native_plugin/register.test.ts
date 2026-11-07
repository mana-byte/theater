import type { Args, On, PromptSubmitResult } from 'claude-code'
import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine, MockClock } from 'claude-code/testing'

const ROOT = '/obs'
const DESC = ROOT + '/native-frontend.json'
const JOURNAL = ROOT + '/native-journal.json'
const TOKEN_FILE = ROOT + '/token'
const SESSION = 'sess-1'
const TOKEN = '0123456789abcdef0123456789abcdef'

type PromptHook = (e: Args<'prompt.submit'>) => PromptSubmitResult | Promise<PromptSubmitResult>

type World = {
  files: Record<string, string>
  writes: string[]
  fetches: string[]
  storeGets: string[]
  storeSets: string[]
  replies: string[]
  clock: MockClock
  setSession: (id: string) => void
  setPrompt: (hook: PromptHook) => void
}

function descriptor(): string {
  return JSON.stringify({
    protocol: 'claude-frontend-v1',
    participant_id: 'p1',
    socket: ROOT + '/frontend.sock',
    token_file: TOKEN_FILE,
    journal: JOURNAL,
    max_result_chars: 1000,
  })
}

function requestReply(request: object): string {
  return JSON.stringify({ received: [], request })
}

function sendRequest(id: string, operation: string, generation = 1): object {
  return {
    type: 'request',
    id,
    method: 'claude.send',
    deadline_ms: 1e15,
    params: { operation_id: operation, session_id: SESSION, generation, prompt: 'do it' },
  }
}

/**
 * The world beneath the plugin: an in-memory fs, the daemon exchange, and a
 * prompt box the test scripts. The journal lives only at the descriptor path.
 */
function world(on: On, seedJournal?: string): World {
  const files: Record<string, string> = { [DESC]: descriptor(), [TOKEN_FILE]: TOKEN }
  if (seedJournal !== undefined) files[JOURNAL] = seedJournal
  let session = SESSION
  const writes: string[] = []
  const fetches: string[] = []
  const storeGets: string[] = []
  const storeSets: string[] = []
  const replies: string[] = []
  let promptHook: PromptHook = e => ({ text: e.text })
  on('fs.exists', (_$, e) => ({ value: e.path in files }))
  on('fs.read', (_$, e) => {
    const text = files[e.path]
    if (text === undefined) throw new Error('ENOENT ' + e.path)
    return { value: text }
  })
  on('fs.write', (_$, e) => {
    writes.push(e.path)
    files[e.path] = e.text
    return { value: undefined }
  })
  on('store.get', (_$, e) => {
    storeGets.push(e.key)
    return { value: undefined }
  })
  on('store.set', (_$, e) => {
    storeSets.push(e.key)
    return { value: undefined }
  })
  on('session.id', () => ({ value: session }))
  on('session.version', () => ({
    value: { version: '2.1.312', base: '2.1.312', builtAt: '2026-01-01T00:00:00Z' },
  }))
  on('http.fetch', (_$, e) => {
    fetches.push(e.init?.body ?? '')
    const text = replies.length ? (replies.shift() as string) : '{"received":[]}'
    return { value: { status: 200, ok: true, headers: {}, text } }
  })
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_$, e) => ({ text: e.answer }))
  on('prompt.submit', (_$, e) => promptHook(e))
  mock.env(on, { THEATER_CLAUDE_FRONTEND_CONFIG: DESC })
  const clock = mock.clock(on, { now: 1_000_000 })
  return {
    files,
    writes,
    fetches,
    storeGets,
    storeSets,
    replies,
    clock,
    setSession: id => { session = id },
    setPrompt: hook => { promptHook = hook },
  }
}

async function start($: Engine, w: World): Promise<void> {
  await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
  await w.clock.advance(100)
}

describe('register', () => {
  test('an owned turn is accepted and journaled to the descriptor path', async ($, on) => {
    const w = world(on)
    w.replies.push(requestReply(sendRequest('r1', 'op1')))
    let release: ((r: PromptSubmitResult) => void) | null = null
    w.setPrompt(e => (e.text === 'do it'
      ? new Promise<PromptSubmitResult>(res => { release = res })
      : { text: e.text }))
    await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
    await w.clock.advance(100)
    await $.turn.start({ text: 'do it', turnId: 't1' })
    release?.({ text: 'do it' })
    await w.clock.advance(100)
    await $.turn.complete({
      answer: 'done', durationMs: 5, isAborted: false, turnId: 't1', reason: 'answer',
    })
    await w.clock.advance(100)
    const all = w.fetches.join('\n')
    expect(all).toContain('"result":"accepted"')
    expect(all).toContain('"native_turn_id":"t1"')
    const journal = JSON.parse(w.files[JOURNAL]) as {
      operations: Record<string, { turn_id: string; outcome?: { terminal: string } }>
    }
    expect(journal.operations.op1.turn_id).toBe('t1')
    expect(journal.operations.op1.outcome?.terminal).toBe('completed')
    expect(w.writes.length).toBeGreaterThan(0)
    expect(new Set(w.writes)).toEqual(new Set([JOURNAL]))
    expect(w.storeGets).toEqual([])
    expect(w.storeSets).toEqual([])
  })

  test('a foreign prompt during admission yields unknown', async ($, on) => {
    const w = world(on)
    w.replies.push(requestReply(sendRequest('r1', 'op1')))
    let release: ((r: PromptSubmitResult) => void) | null = null
    w.setPrompt(e => (e.text === 'do it'
      ? new Promise<PromptSubmitResult>(res => { release = res })
      : { text: e.text }))
    await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
    await w.clock.advance(100)
    await $.prompt.submit({ text: 'human typed' })
    await $.turn.start({ text: 'do it', turnId: 't1' })
    release?.({ text: 'do it' })
    await w.clock.advance(100)
    const all = w.fetches.join('\n')
    expect(all).toContain('"result":"unknown"')
    expect(all).toContain('"error_code":"uncorrelated"')
    const journal = JSON.parse(w.files[JOURNAL]) as { operations: object }
    expect(journal.operations).toEqual({})
  })

  test('a turn with different text inside the admission window yields unknown', async ($, on) => {
    const w = world(on)
    w.replies.push(requestReply(sendRequest('r1', 'op1')))
    let release: ((r: PromptSubmitResult) => void) | null = null
    w.setPrompt(e => (e.text === 'do it'
      ? new Promise<PromptSubmitResult>(res => { release = res })
      : { text: e.text }))
    await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
    await w.clock.advance(100)
    await $.turn.start({ text: 'queued human prompt', turnId: 't9' })
    release?.({ text: 'do it' })
    await w.clock.advance(100)
    const all = w.fetches.join('\n')
    expect(all).toContain('"result":"unknown"')
    expect(all).toContain('"error_code":"uncorrelated"')
    const journal = JSON.parse(w.files[JOURNAL]) as { operations: object }
    expect(journal.operations).toEqual({})
  })

  test('a prompt still entering when the send is checked yields busy', async ($, on) => {
    const w = world(on)
    w.replies.push(requestReply(sendRequest('r1', 'op1')))
    let releaseHuman: (() => void) | null = null
    w.setPrompt(e => (e.text === 'human typed'
      ? new Promise<PromptSubmitResult>(res => { releaseHuman = () => res({ text: e.text }) })
      : { text: e.text }))
    await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
    const human = $.prompt.submit({ text: 'human typed' })
    await w.clock.settle()
    await w.clock.advance(100)
    releaseHuman?.()
    await human
    await w.clock.advance(100)
    const all = w.fetches.join('\n')
    expect(all).toContain('"result":"rejected"')
    expect(all).toContain('"error_code":"busy"')
    const journal = JSON.parse(w.files[JOURNAL]) as { operations: object }
    expect(journal.operations).toEqual({})
  })

  test('a session change fails unresolved operations', async ($, on) => {
    const w = world(on)
    w.replies.push(requestReply(sendRequest('r1', 'op1')))
    let release: ((r: PromptSubmitResult) => void) | null = null
    w.setPrompt(e => (e.text === 'do it'
      ? new Promise<PromptSubmitResult>(res => { release = res })
      : { text: e.text }))
    await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
    await w.clock.advance(100)
    await $.turn.start({ text: 'do it', turnId: 't1' })
    release?.({ text: 'do it' })
    await w.clock.advance(100)
    w.setSession('sess-2')
    await w.clock.advance(100)
    const journal = JSON.parse(w.files[JOURNAL]) as {
      operations: Record<string, { outcome?: { terminal: string; error_code?: string } }>
    }
    expect(journal.operations.op1.outcome?.terminal).toBe('failed')
    expect(journal.operations.op1.outcome?.error_code).toBe('native_session_changed')
    expect(w.fetches.join('\n')).toContain('"state":"unknown"')
  })

  test('claude.ack prunes acknowledged outcomes', async ($, on) => {
    const seed = JSON.stringify({
      session_id: SESSION,
      generation: 1,
      revision: 4,
      active: null,
      terminal: 't0',
      operations: {
        op0: {
          turn_id: 't0',
          outcome: {
            session_id: SESSION, turn_id: 't0', terminal: 'completed',
            result: 'ok', completed_at: 1,
          },
        },
      },
    })
    const w = world(on, seed)
    w.replies.push(requestReply({
      type: 'request',
      id: 'r1',
      method: 'claude.ack',
      deadline_ms: 1e15,
      params: { session_id: SESSION, turn_ids: ['t0'] },
    }))
    await start($, w)
    await w.clock.advance(100)
    const journal = JSON.parse(w.files[JOURNAL]) as { generation: number; operations: object }
    expect(journal.generation).toBe(2)
    expect(journal.operations).toEqual({})
    expect(w.fetches.join('\n')).toContain('"acknowledged":true')
  })

  test('a restored journal increments generation and restores active', async ($, on) => {
    const seed = JSON.stringify({
      session_id: SESSION,
      generation: 5,
      revision: 3,
      active: 't9',
      terminal: 't8',
      operations: {},
    })
    const w = world(on, seed)
    await start($, w)
    const journal = JSON.parse(w.files[JOURNAL]) as { generation: number; active: string }
    expect(journal.generation).toBe(6)
    expect(journal.active).toBe('t9')
    const all = w.fetches.join('\n')
    expect(all).toContain('"active_turn_id":"t9"')
    expect(all).toContain('"state":"active"')
  })
})
