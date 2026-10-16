"""Small real GTK desktop fixture; its event journal verifies effects independently of receipts."""

import json
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

path = sys.argv[1]


def record(**event):
    with open(path, "a") as stream:
        stream.write(json.dumps(event) + "\n")


window = Gtk.Window(title="mai-ui Linux fixture")
window.set_default_size(700, 500)
window.connect("destroy", Gtk.main_quit)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
window.add(box)
entry = Gtk.Entry()
entry.get_accessible().set_name("Fixture input")
entry.connect("changed", lambda w: record(text=w.get_text()))
box.pack_start(entry, False, False, 0)
button = Gtk.Button(label="Fixture button")
button.connect("clicked", lambda w: record(clicked=True))
box.pack_start(button, False, False, 0)
area = Gtk.DrawingArea()
area.set_size_request(300, 150)
area.add_events(
    Gdk.EventMask.BUTTON_PRESS_MASK
    | Gdk.EventMask.BUTTON_RELEASE_MASK
    | Gdk.EventMask.POINTER_MOTION_MASK
    | Gdk.EventMask.SCROLL_MASK
)
area.get_accessible().set_name("Pointer area")
area.connect("button-press-event", lambda w, e: record(button=e.button, kind="down"))
area.connect("button-release-event", lambda w, e: record(button=e.button, kind="up"))
area.connect("scroll-event", lambda w, e: record(scroll=int(e.direction)))
box.pack_start(area, False, False, 0)
scroll = Gtk.ScrolledWindow()
text = Gtk.TextView()
text.get_accessible().set_name("Fixture document")
text.get_buffer().set_text("Line\n" * 200)
scroll.add(text)
box.pack_start(scroll, True, True, 0)
window.show_all()
entry.grab_focus()
Gtk.main()
