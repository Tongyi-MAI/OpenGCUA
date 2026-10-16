# core — resource model / observation delivery / staleness

Public alias over `mai_ui.unified`: the device-agnostic resource model
(Runtime → Session → Device → App → Window → UI{ax, vision, input}), observation
delivery (an observation is not visible to the model until explicitly delivered), and
staleness (references to a changed screen are rejected, not guessed).
