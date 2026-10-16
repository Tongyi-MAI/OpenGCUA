# Linux X11 adapter

Optional private Python worker for the embedded Rust runtime. `python-xlib==0.33` provides XTEST input and XFixes cursor capture; `Pillow==11.3.0` captures the explicitly selected X11 screen through XCB. Ubuntu's system pyatspi/PyGObject packages provide accessibility in a killable helper.

Install with `mu setup linux` or `mu-js setup linux` inside Ubuntu. No dependency is imported by unrelated platform providers. The worker speaks the existing versioned JSON-lines provider protocol, including independently delivered cancellation and events. It has no listening socket or agent-facing raw automation API.

On an Ubuntu X11 desktop, install the system dependencies and configure the worker:

```bash
sudo apt-get install python3-venv python3-pyatspi python3-gi gir1.2-atspi-2.0 libxcb1 libx11-6 xclip
mu setup linux --python /usr/bin/python3
mu doctor linux --python /usr/bin/python3
```

`DISPLAY`, `XAUTHORITY`, and the graphical user's D-Bus environment must identify
that desktop session. Native Wayland desktop control is not supported. The
`tests/` directory contains optional Xvfb/GTK/Qt/LibreOffice fixtures.
