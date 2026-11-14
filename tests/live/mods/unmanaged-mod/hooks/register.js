// Live-test mod in a user location (--plugin-dir). allowManagedModsOnly must refuse it, so
// neither hook may ever run and the marker must never appear.
export function register(on) {
  on('session.start', async ($, e, next) => {
    await $.fs.write('/tmp/ccw-live-unmanaged-mod.marker', 'session.start\n')
    return next(e)
  })
  on('tool.call', async ($, e, next) => {
    await $.fs.write('/tmp/ccw-live-unmanaged-mod.marker', 'tool.call ' + e.tool + '\n')
    return next(e)
  })
}
