FROM ubuntu:22.04@sha256:281c5745f657873d78e5531fc5ba8575f46ab7769b94550ac99543f122679986

ARG DEBIAN_FRONTEND=noninteractive
ARG PYTHON_VERSION=3.12.14
ARG PYTHON_SHA256=5c8462af5790baf43a321a1559dbe0db06d1be4300fb85fb53c40060668e548a

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        appstream \
        binutils \
        build-essential \
        ca-certificates \
        curl \
        desktop-file-utils \
        dpkg-dev \
        file \
        git \
        libbz2-dev \
        libdb-dev \
        libegl1 \
        libffi-dev \
        libfontconfig1 \
        libgdbm-dev \
        libgl1 \
        libglib2.0-0 \
        libgtk-3-0 \
        liblzma-dev \
        libncursesw5-dev \
        libreadline-dev \
        libsqlite3-dev \
        libssl-dev \
        libudev-dev \
        libusb-1.0-0 \
        libusb-1.0-0-dev \
        libxcb-cursor0 \
        libxcb-icccm4 \
        libxcb-keysyms1 \
        libxcb-shape0 \
        libxcb-xinerama0 \
        libxkbcommon-x11-0 \
        patchelf \
        pkg-config \
        uuid-dev \
        xz-utils \
        zlib1g-dev \
        zsync \
    && printf '%s\n' \
        'deb-src http://archive.ubuntu.com/ubuntu jammy main restricted universe multiverse' \
        'deb-src http://archive.ubuntu.com/ubuntu jammy-updates main restricted universe multiverse' \
        'deb-src http://security.ubuntu.com/ubuntu jammy-security main restricted universe multiverse' \
        > /etc/apt/sources.list.d/mc7-studio-sources.list \
    && apt-get update

WORKDIR /usr/src/python
RUN curl --fail --location --silent --show-error --output Python.tar.xz "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
    && echo "${PYTHON_SHA256}  Python.tar.xz" | sha256sum --check --strict \
    && mkdir source \
    && tar --extract --xz --file Python.tar.xz --directory source --strip-components=1 \
    && cd source \
    && ./configure --prefix=/opt/python --enable-shared --with-ensurepip=install \
    && make -j"$(nproc)" \
    && make altinstall \
    && install -m 0644 LICENSE /opt/python/LICENSE.txt \
    && cd / \
    && rm -rf /usr/src/python

ENV PATH=/opt/python/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/python/lib

RUN python3.12 -m pip install --no-cache-dir --no-binary=hidapi \
        'PyInstaller==6.22.3' \
        'pyinstaller-hooks-contrib==2026.7' \
        'altgraph==0.17.5' \
        'packaging==26.3' \
        'setuptools==84.0.0' \
        'build==1.6.1' \
        'pyproject-hooks==1.2.0' \
        'wheel==0.48.0' \
        'PySide6==6.11.2' \
        'hidapi==0.15.0' \
        'psutil==7.2.2' \
        'websocket-client==1.9.2' \
        'certifi==2026.7.22'

WORKDIR /workspace
COPY . /workspace
CMD ["bash"]
