FROM quay.io/jumpstarter-dev/jumpstarter:latest
WORKDIR /app

COPY target-no-can.gz /app/target.gz
COPY jumpstarter_driver_rcars4/*.py /app/jumpstarter_driver_rcars4/
COPY pyproject.toml /app/
COPY initramfs-debug.img /app/
COPY Image /app/
COPY r8a779f0-spider.dtb /app/

RUN dnf install -y uv git python-pip

ENV PYTHONPATH=$PYTHONPATH:/jumpstarter/lib/python3.12/site-packages/
RUN uv build .
RUN pip install dist/*.whl
