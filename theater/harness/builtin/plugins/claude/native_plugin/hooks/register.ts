import type { EngineInterface, On } from 'claude-code'

type Config = {
  protocol: string
  participant_id: string
  socket: string
  token_file: string
  journal: string
  max_result_chars: number
}
type Outcome = {
  session_id: string
  turn_id: string
  terminal: 'completed' | 'failed' | 'interrupted'
  result: string | null
  completed_at: number
  error_code?: string
  error?: string
}
type Operation = { turn_id: string; outcome?: Outcome }
type Journal = {
  generation: number
  revision: number
  session_id: string
  active: string | null
  terminal: string | null
  operations: Record<string, Operation>
}
type Admission = {
  operation_id: string
  prompt: string
  session_id: string
  generation: number
  turns: string[]
  foreign: boolean
  ended: Outcome | null
}
type Request = {
  type: string
  id: string
  method: string
  deadline_ms: number
  params: Record<string, unknown>
}

const PROTOCOL = 'claude-frontend-v1'
const JOURNAL_BYTES = 2 * 1024 * 1024
const RESERVED_OUTCOME_BYTES = 400_000
const MAX_OPERATIONS = 32
let config: Config | null = null
let token = ''
let version = ''
let journal: Journal | null = null
let active: string | null = null
let fault: string | null = null
let admission: Admission | null = null
// Prompts whose prompt.submit chain is still running; a send must not be
// admitted while one is entering, because its turn has not started yet.
let entering = 0
let polling = false
let saving: Promise<void> = Promise.resolve()
let responses: Record<string, object> = {}

function identifier(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 128
}

function byteLength(text: string): number {
  return new TextEncoder().encode(text).length
}

function size(value: unknown): number {
  return byteLength(JSON.stringify(value))
}

async function persist($: EngineInterface): Promise<void> {
  if (!journal || !config) return
  const image = JSON.stringify(journal)
  if (byteLength(image) > JOURNAL_BYTES) {
    fault = 'native journal is full; awaiting durable acknowledgements'
    throw Error(fault)
  }
  const path = config.journal
  saving = saving.catch(() => {}).then(() => $.fs.write(path, image))
  try {
    await saving
    fault = null
  } catch {
    fault = 'native journal could not be persisted; controls unavailable'
    throw Error(fault)
  }
}

async function snapshot($: EngineInterface): Promise<object> {
  if (!journal) throw Error('native bridge not initialized')
  const current = await $.session.id()
  const changedSession = current !== journal.session_id
  if (changedSession) {
    let changed = false
    for (const op of Object.values(journal.operations)) {
      if (!op.outcome) {
        op.outcome = {
          session_id: journal.session_id,
          turn_id: op.turn_id,
          terminal: 'failed',
          result: null,
          completed_at: Date.now() / 1000,
          error_code: 'native_session_changed',
          error: 'Claude changed conversations before completion was confirmed; not replayed.',
        }
        changed = true
      }
    }
    if (changed) {
      journal.revision++
      await persist($)
    }
  }
  const sessionError = changedSession
    ? 'native session changed; resume through Theater to bind a new scope'
    : null
  return {
    protocol: PROTOCOL,
    session_id: journal.session_id,
    current_session_id: current,
    generation: journal.generation,
    revision: journal.revision,
    active_turn_id: changedSession ? null : active,
    state: fault || changedSession ? 'unknown' : active ? 'active' : 'idle',
    version,
    error: fault ?? sessionError,
  }
}

function receipt(operation: string, result: string, extra: object = {}): object {
  return {
    operation_id: operation,
    result,
    session_id: journal?.session_id,
    generation: journal?.generation,
    ...extra,
  }
}

function busy(operation: string): object {
  return receipt(operation, 'rejected', { error_code: 'busy', error: 'Claude is not idle' })
}

async function submit($: EngineInterface, params: Record<string, unknown>): Promise<object> {
  if (!journal || !config) throw Error('native bridge not initialized')
  const operation = params.operation_id
  if (!identifier(operation) || typeof params.prompt !== 'string' || !params.prompt.trim()) {
    throw Error('invalid native send')
  }
  const known = Object.hasOwn(journal.operations, operation)
    ? journal.operations[operation]
    : undefined
  if (known) return receipt(operation, 'accepted', { native_turn_id: known.turn_id })
  if (active || admission || entering > 0) return busy(operation)
  const full = Object.keys(journal.operations).length >= MAX_OPERATIONS
    || size(journal) > JOURNAL_BYTES - RESERVED_OUTCOME_BYTES
  if (fault || full) {
    return receipt(operation, 'rejected', {
      error_code: 'not_ready',
      error: 'Native evidence buffer must drain before another send',
    })
  }
  const current = await $.session.id()
  if (entering > 0) return busy(operation)
  if (current !== journal.session_id || active || admission) {
    return receipt(operation, 'rejected', {
      error_code: 'stale_target',
      error: 'Claude changed session or activity before admission',
    })
  }
  const scope: Admission = {
    operation_id: operation,
    prompt: params.prompt,
    session_id: current,
    generation: journal.generation,
    turns: [],
    foreign: false,
    ended: null,
  }
  admission = scope
  try {
    const answer = await $.prompt.submit({ text: params.prompt })
    const owned = admission === scope
    const turns = [...scope.turns]
    const foreign = scope.foreign
    if (owned) admission = null
    if (answer.drop) {
      return receipt(operation, 'rejected', {
        error_code: 'native_refusal',
        error: answer.drop.slice(0, 512),
      })
    }
    const uncorrelated = !owned
      || turns.length !== 1
      || foreign
      || journal.generation !== scope.generation
    if (uncorrelated) {
      return receipt(operation, 'unknown', {
        error_code: 'uncorrelated',
        error:
          'Native submission could not be tied to exactly one unchanged main turn; not replayed',
      })
    }
    // Install the exact owner before the next await, so a fast completion cannot pass between them.
    journal.operations[operation] = {
      turn_id: turns[0],
      ...(scope.ended ? { outcome: scope.ended } : {}),
    }
    if ((await $.session.id()) !== scope.session_id) {
      return receipt(operation, 'unknown', {
        error_code: 'stale_target',
        error: 'Native session changed during admission; not replayed',
      })
    }
    journal.revision++
    await persist($)
    return receipt(operation, 'accepted', { native_turn_id: turns[0] })
  } catch (error) {
    return receipt(operation, 'unknown', {
      error_code: 'native_error',
      error: String(error).slice(0, 512),
    })
  } finally {
    if (admission === scope) admission = null
  }
}

async function handle($: EngineInterface, request: Request): Promise<object> {
  if (!journal || !identifier(request.id) || request.type !== 'request' || !request.params) {
    throw Error('invalid native request')
  }
  if (!Number.isFinite(request.deadline_ms) || Date.now() >= request.deadline_ms) {
    throw Error('native request expired before execution')
  }
  const params = request.params
  if (request.method === 'claude.snapshot') return snapshot($)
  if (request.method === 'claude.ack') {
    const ids = params.turn_ids
    const validIds = Array.isArray(ids)
      && ids.length <= MAX_OPERATIONS
      && ids.every(x => identifier(x))
    if (params.session_id !== journal.session_id || !validIds) {
      throw Error('invalid native evidence acknowledgement')
    }
    const turns = new Set(ids)
    for (const [key, op] of Object.entries(journal.operations)) {
      if (op.outcome && turns.has(op.turn_id)) delete journal.operations[key]
    }
    journal.revision++
    await persist($)
    return { acknowledged: true }
  }
  if (!identifier(params.operation_id)) throw Error('invalid operation id')
  const current = await $.session.id()
  const stale = params.session_id !== journal.session_id
    || params.generation !== journal.generation
    || current !== journal.session_id
  if (stale) {
    return receipt(params.operation_id, 'rejected', {
      error_code: 'stale_target',
      error: 'Native session or bridge instance changed',
    })
  }
  if (request.method === 'claude.send') return submit($, params)
  if (request.method === 'claude.interrupt') {
    const turn = params.native_turn_id
    if (!identifier(turn) || turn !== active) {
      return receipt(params.operation_id, 'rejected', {
        error_code: 'stale_target',
        error: 'The requested native turn is not active',
      })
    }
    try {
      await $.turn.abort({ turnId: turn })
      return receipt(params.operation_id, 'accepted', { native_turn_id: turn })
    } catch (error) {
      return receipt(params.operation_id, 'rejected', {
        error_code: 'stale_target',
        error: String(error).slice(0, 512),
      })
    }
  }
  throw Error('native control is not supported')
}

async function poll($: EngineInterface): Promise<void> {
  if (polling || !journal || !config) return
  polling = true
  try {
    const state = await snapshot($)
    const outcomes = Object.values(journal.operations)
      .flatMap(op => (op.outcome ? [op.outcome] : []))
      .slice(0, 2)
    const messages = [
      { type: 'snapshot', ...state },
      ...(outcomes.length ? [{ type: 'history', outcomes }] : []),
      ...Object.values(responses),
    ]
    const body = JSON.stringify({
      type: 'hello',
      protocol: 'theater-frontend-v1',
      token,
      generation: journal.generation,
      messages,
    })
    const response = await $.http.fetch('http://theater/exchange', {
      socketPath: config.socket,
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    })
    if (!response.ok) return
    const reply = JSON.parse(response.text)
    for (const id of reply.received ?? []) delete responses[id]
    const request = reply.request
    if (request && !responses[request.id]) {
      try {
        const result = await handle($, request)
        responses[request.id] = { type: 'response', id: request.id, result }
      } catch (error) {
        responses[request.id] = {
          type: 'response',
          id: request.id,
          error: { code: 'native_request_failed', message: String(error).slice(0, 512) },
        }
      }
    }
    if (Object.keys(responses).length > 32) throw Error('native response buffer is full')
  } catch {
    // The journal survives a missing daemon; never replay a mutation to recover the connection.
  } finally {
    polling = false
  }
}

function validJournal(value: Journal): boolean {
  const header = identifier(value.session_id)
    && Number.isSafeInteger(value.generation)
    && value.generation >= 1
    && Number.isSafeInteger(value.revision)
    && value.revision >= 0
    && size(value) <= JOURNAL_BYTES
  if (!header) return false
  if ((value.active !== null && !identifier(value.active))
    || (value.terminal !== null && !identifier(value.terminal))) return false
  const ops = value.operations
  if (!ops || typeof ops !== 'object' || Array.isArray(ops)
    || Object.keys(ops).length > MAX_OPERATIONS) return false
  return Object.entries(ops).every(([key, op]) => {
    if (!identifier(key) || !op || !identifier(op.turn_id)) return false
    const o = op.outcome
    if (!o) return true
    return o.session_id === value.session_id
      && o.turn_id === op.turn_id
      && ['completed', 'failed', 'interrupted'].includes(o.terminal)
      && Number.isFinite(o.completed_at)
      && (o.result === null || typeof o.result === 'string')
  })
}

async function initialize($: EngineInterface): Promise<void> {
  const path = await $.env.get('THEATER_CLAUDE_FRONTEND_CONFIG')
  if (!path) return
  const data = JSON.parse(await $.fs.read(path))
  const valid = data.protocol === PROTOCOL
    && identifier(data.participant_id)
    && typeof data.socket === 'string'
    && data.socket.startsWith('/')
    && typeof data.token_file === 'string'
    && data.token_file.startsWith('/')
    && typeof data.journal === 'string'
    && data.journal.startsWith('/')
    && Number.isSafeInteger(data.max_result_chars)
    && data.max_result_chars > 0
  if (!valid) throw Error('invalid Theater native descriptor')
  config = data
  token = (await $.fs.read(data.token_file)).trim()
  if (token.length < 16 || token.length > 256) throw Error('invalid Theater native credential')
  version = (await $.session.version()).version
  const current = await $.session.id()
  let stored: Journal | undefined
  if (await $.fs.exists(data.journal)) {
    stored = JSON.parse(await $.fs.read(data.journal)) as Journal
    if (!validJournal(stored)) throw Error('native journal has an invalid scope or record')
  }
  journal = stored ?? {
    session_id: current,
    generation: 0,
    revision: 0,
    active: null,
    terminal: null,
    operations: {},
  }
  journal.generation++
  journal.revision++
  active = journal.active === journal.terminal ? null : journal.active
  if (!Number.isSafeInteger(journal.generation) || journal.generation <= 0) {
    throw Error('native bridge generation exhausted')
  }
  await persist($)
  $.clock.every(100, () => { void poll($) })
}

export function register(on: On): void {
  on('session.start', async ($, event, next) => {
    await initialize($)
    return next(event)
  })
  on('prompt.submit', async ($, event, next) => {
    entering++
    try {
      if (admission) admission.foreign = true
      return await next(event)
    } finally {
      entering--
    }
  })
  on('turn.start', async ($, event, next) => {
    if (journal && !('agentId' in event && event.agentId)) {
      active = event.turnId
      journal.active = active
      journal.revision++
      if (admission) {
        admission.turns.push(event.turnId)
        // A queued prompt's turn can start mid-admission; only the submitted text owns it.
        if (!event.text.includes(admission.prompt)) admission.foreign = true
      }
      await persist($)
    }
    return next(event)
  })
  on('turn.complete', async ($, event, next) => {
    if (!journal || event.agentId) return next(event)
    const tooLarge = config && Array.from(event.answer).length > config.max_result_chars
    const failed = tooLarge || event.reason === 'error' || event.reason === 'refusal'
    const outcome: Outcome = {
      session_id: journal.session_id,
      turn_id: event.turnId,
      terminal: failed ? 'failed' : event.isAborted ? 'interrupted' : 'completed',
      result: tooLarge ? null : event.answer,
      completed_at: Date.now() / 1000,
      ...(tooLarge
        ? {
            error_code: 'native_result_too_large',
            error: 'Native result exceeded the bounded result contract; not truncated',
          }
        : {}),
    }
    if (admission?.turns.includes(event.turnId)) admission.ended = outcome
    for (const op of Object.values(journal.operations)) {
      if (op.turn_id === event.turnId && !op.outcome) op.outcome = outcome
    }
    journal.terminal = event.turnId
    journal.revision++
    await persist($)
    const result = await next(event)
    if (active === event.turnId) {
      active = null
      journal.revision++
    }
    return result
  })
}
