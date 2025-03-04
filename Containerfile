FROM quay.io/jumpstarter-dev/jumpstarter:latest
WORKDIR /app


# TODO: get these from some URL or something, this is not reproducible
#COPY target-no-can.gz /app/target.gz
COPY initramfs-debug.img /app/
COPY Image /app/
COPY r8a779f0-spider.dtb /app/

RUN dnf install -y uv git python-pip
COPY jumpstarter_driver_rcars4/*.py /app/jumpstarter_driver_rcars4/
COPY pyproject.toml /app/


ENV PYTHONPATH=$PYTHONPATH:/jumpstarter/lib/python3.12/site-packages/
RUN uv clean
RUN uv build
RUN pip install dist/*.whl
