export default {
  id: __PLUGIN_ID__,
  setup({ app, renderer }) {
    if (!__QUALIFIED_VERSIONS__.includes(app.version)) return
    const root = renderer.root
    const original = root.render
    function render(buffer, deltaTime) {
      try {
        return original.call(this, buffer, deltaTime)
      } catch (error) {
        // OpenTUI skips the viewport's pop commands when a diff render throws.
        buffer.clearScissorRects()
        buffer.clearOpacity()
        renderer.clearHitGridScissorRects()
        renderer.requestRender()
        throw error
      }
    }
    root.render = render
    return () => {
      if (root.render === render) root.render = original
    }
  },
}
