// Live-test mod, enabled only through the managed overlay (prependPlugins).
// A Bash call whose command contains "live-mod" is answered here without calling next,
// so the tool never runs and no non-managed PreToolUse hook runs either.
export function register(on) {
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const command = String(e.command || '')
    if (!command.includes('live-mod')) return next(e)
    await $.fs.write('/tmp/ccw-live-managed-mod.marker', 'answered: ' + command + '\n')
    // The result takes the Bash tool's output shape.
    return { result: { stdout: 'live-managed-mod answered: ' + command, stderr: '', interrupted: false } }
  })
}
