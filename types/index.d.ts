// rolling-context's types contract: what the mod keeps in $.state.

/** The proxy's /health answer, the fields the mod reads. */
export type RollingContextHealth = {
  service?: string
  version?: string
  enabled?: boolean
  compression_count?: number
  total_tokens_saved?: number
  stored_compressions?: number
  active_compressions?: number
  /** This session's state, and (proxy 1.14.9 on) its own compressions. */
  session?: { enabled: boolean; scope: string; compressions?: number; tokens_saved?: number }
}

/** The /config options as the settings pane shows them. */
export type RollingContextSettings = {
  trigger: number
  target: number
  retention: string
  retentionOptions: string[]
}

/** One of the desktop app's accounts, as move_session.py lists it. */
export type RollingContextAccount = {
  account: string
  org: string | null
  email: string | null
  /** The nickname given in the Move session pane. */
  name: string | null
  sessions: number
  latestTitle: string | null
  latestAt: number | null
  latestArchived?: boolean
}

/** A move that waits for this session to be archived in the account it leaves. */
export type RollingContextPendingMove = {
  state: string
  at: number
  title: string
  from: { account: string; org: string }
  to: { account: string; org: string }
  backup?: string
}

/** move_session.py accounts: this session's record, the accounts, any move. */
export type RollingContextMove = {
  ok: boolean
  session: {
    account: string
    org: string
    localId: string
    title: string
    cwd: string
    kind: 'folder' | 'scratch' | 'worktree'
    transcript: string | null
  } | null
  accounts: RollingContextAccount[]
  pending: RollingContextPendingMove | null
  /** Why this session cannot move, else null. */
  blocked: string | null
}

/** One session of another account, as move_session.py sessions lists it. */
export type RollingContextImportSession = {
  localId: string
  cliSessionId: string | null
  org: string
  title: string
  cwd: string | null
  kind: 'folder' | 'scratch' | 'worktree'
  archived: boolean
  lastActivityAt: number | null
  /** Why it cannot be imported (its CLI runs, no transcript), else null. */
  blocked: string | null
}

/** move_session.py sessions: one account's sessions, live ones first. */
export type RollingContextImportList = {
  ok: boolean
  account: string
  sessions: RollingContextImportSession[]
}

declare module 'claude-code' {
  interface PluginState {
    'rolling-context': {
      health: RollingContextHealth | null
      isOpen: boolean
      settings: RollingContextSettings | null
      /** What the row says while a control works (a restart), else null. */
      busy: string | null
      /** Context tokens from $.session.usage, else null. */
      ctx: number | null
      /** The Move session pane's data: move_session.py's accounts answer. */
      move: RollingContextMove | null
      /** The Move session box above the prompt, where no pane is placed. */
      moveOpen: boolean
      /** The account picked in the Move session pane. */
      moveTo: string | null
      /** What the Move session pane says while a move works, else null. */
      moveBusy: string | null
      /** The account the Move session pane imports from. */
      importFrom: string | null
      /** That account's sessions, move_session.py's sessions answer. */
      importList: RollingContextImportList | null
      /** The session picked to import, by its record id. */
      importPick: string | null
      /** Whether the import list shows archived sessions too. */
      importArchived: boolean
      /** What the import section says while an import works, else null. */
      importBusy: string | null
    }
  }
}
