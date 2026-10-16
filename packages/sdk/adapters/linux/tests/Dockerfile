FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-pip python3-pyatspi python3-gi python3-tk gir1.2-gtk-3.0 gir1.2-atspi-2.0 at-spi2-core dbus-x11 xvfb openbox x11-xserver-utils xclip xauth libxcb1 python3-pyqt5 libreoffice-writer libreoffice-calc libreoffice-gtk3 ca-certificates curl build-essential pkg-config && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv --system-site-packages /opt/test
COPY adapters/linux/requirements.lock /tmp/requirements.lock
RUN /opt/test/bin/pip install -r /tmp/requirements.lock pytest pytest-asyncio maturin
ENV PATH=/opt/test/bin:$PATH
WORKDIR /sdk
