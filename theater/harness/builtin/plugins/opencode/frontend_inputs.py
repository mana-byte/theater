"""Content-free pending-dialog projection for the stock OpenCode TUI extension."""

TUI_INPUTS = """
function observeInputs(api, changed) {
  const maxSessions = 128
  let scope = null
  let flight = null
  let stopped = false
  const boundedID = (id) => typeof id === "string" && id.length > 0 && id.length <= 512
  const remember = (session) => {
    if (scope && session?.parentID === scope.id && boundedID(session.id)
      && scope.children.size < maxSessions) scope.children.add(session.id)
  }
  const refresh = () => {
    if (stopped || !scope || flight || !api.client?.session?.children) return
    const current = scope
    const controller = new AbortController()
    const deadline = setTimeout(() => controller.abort(), 1500)
    flight = controller
    void (async () => {
      try {
        const result = await api.client.session.children(
          { sessionID: current.id }, { signal: controller.signal },
        )
        if (stopped || scope !== current || controller.signal.aborted
          || result?.error || !Array.isArray(result?.data)) return
        for (const session of result.data.slice(0, maxSessions)) remember(session)
        changed()
      } catch {} finally {
        clearTimeout(deadline)
        if (flight === controller) flight = null
      }
    })()
  }
  for (const type of ["permission.asked", "permission.replied", "question.asked",
    "question.replied", "question.rejected"]) {
    api.event.on(type, (event) => {
      if (stopped) return
      const id = event?.properties?.sessionID
      if (boundedID(id) && scope && id !== scope.id) {
        const session = api.state.session.get?.(id)
        remember(session)
        if (!session && scope.children.size < maxSessions) scope.children.add(id)
      }
      changed()
    })
  }
  return {
    counts(id, epoch) {
      if (scope?.id !== id || scope?.epoch !== epoch) {
        flight?.abort()
        flight = null
        scope = id ? { id, epoch, children: new Set() } : null
        refresh()
      }
      const counts = { permission_count: 0, question_count: 0 }
      if (!scope || api.state.session.get?.(id)?.parentID) return counts
      // Stock OpenCode renders direct-child dialogs in the parent route.
      const sessions = [id, ...scope.children].filter((child) =>
        child === id || api.state.session.get?.(child)?.parentID === id,
      )
      for (const session of sessions) {
        counts.permission_count += api.state.session.permission(session).length
        counts.question_count += api.state.session.question(session).length
      }
      return counts
    },
    dispose() {
      stopped = true
      flight?.abort()
      flight = null
      scope = null
    },
  }
}
"""
