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
    }
  }
}
