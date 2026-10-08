# The digest is the verified linux/amd64 manifest for Python 3.12.15 slim Bookworm.
FROM python:3.12.15-slim-bookworm@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/linode-image-lab/src

WORKDIR /opt/linode-image-lab
COPY pyproject.toml ./
COPY src/ ./src/
COPY policy/ ./policy/
COPY src/linode_image_lab/container_entrypoint.py /usr/local/bin/linode-image-lab

USER 65532:65532
ENTRYPOINT ["linode-image-lab"]
