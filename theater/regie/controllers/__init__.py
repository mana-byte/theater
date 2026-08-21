"""Régie controllers: state/mechanics extracted from the composition root.

A controller owns a cohesive piece of behavior that grew too large for
``RegieApp`` but is not a widget, a renderer, or a pure constant.
``RegieApp`` remains the Textual composition and binding root; controllers
hold no reference to it.
"""
