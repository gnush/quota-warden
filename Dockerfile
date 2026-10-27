FROM python:3.11-slim

# kopf only ever talks to the API server, so no build toolchain is needed at
# runtime; kopf and kubernetes ship wheels.
WORKDIR /app
COPY pyproject.toml ./
COPY operator/ operator/
RUN pip install --no-cache-dir .

# Without this, kopf's stdout is block-buffered when not on a tty: `kubectl logs`
# would show nothing until the buffer fills.
ENV PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 warden
# numeric: kubelet cannot verify a named user is non-root against runAsNonRoot
USER 10001

# Peering needs the ClusterKopfPeering CRD (crds/clusterkopfpeering.yaml), and
# without it the operator will not sweep: it sets peering.mandatory.
# Extra kopf flags append to these, e.g. `docker run <image> --verbose`.
ENTRYPOINT ["kopf", "run", "--all-namespaces", "-m", "quota_warden.handlers"]
