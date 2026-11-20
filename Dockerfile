# syntax=docker/dockerfile:1.7
# Pinned x86_64 project image. Bump the image digest and
# Ubuntu snapshot together after validating the package set.
ARG UBUNTU_IMAGE=ubuntu@sha256:a2d43bffc46670d7108bb3c0468c67c5557c4c92e83e1e34cbff1322ad1b94dc
ARG UBUNTU_SNAPSHOT=20261117T000000Z
ARG GOVC_VERSION=0.33.0
ARG AWS_CLI_VERSION=2.35.21
ARG GOOGLE_CLOUD_CLI_VERSION=594.0.0
ARG GOOGLE_CLOUD_CLI_SHA256=1f7233537c26bba94a3ea3f9085a60396745eaf44b21b8454fc6fdd15bb6fcde

# The minimal Ubuntu base has no system CA bundle. Bootstrap trust through
# Ubuntu's signed default archive (HTTP transport, signed package indexes),
# then pin this package back to the selected snapshot in every build stage.
FROM --platform=linux/amd64 ${UBUNTU_IMAGE} AS ca-bootstrap
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

# Build govc from its pinned Go module version. The Go module checksum database
# verifies the source used by `go install`.
FROM ca-bootstrap AS govc-builder
ARG UBUNTU_SNAPSHOT
ARG GOVC_VERSION
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get -S "${UBUNTU_SNAPSHOT}" update \
  && ca_version="$(apt-cache policy ca-certificates | sed -n 's/ *Candidate: //p')" \
  && test -n "${ca_version}" && test "${ca_version}" != '(none)' \
  && apt-get -S "${UBUNTU_SNAPSHOT}" install -y --no-install-recommends --allow-downgrades \
  "ca-certificates=${ca_version}" golang-go \
  && rm -rf /var/lib/apt/lists/*
RUN GOTOOLCHAIN=local CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go install "github.com/vmware/govmomi/govc@v${GOVC_VERSION}" \
  && install -D -m 0755 /root/go/bin/govc /out/usr/local/bin/govc

# Build the VDDK plugin from the exact Ubuntu source package paired with the
# runtime nbdkit package. OpenVDDK itself remains a checked, on-demand install
# under the shared artifact root.
FROM ca-bootstrap AS vddk-plugin-builder
ARG UBUNTU_SNAPSHOT
ENV DEBIAN_FRONTEND=noninteractive
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/ubuntu.sources \
  && sed -i 's/^Components: main$/Components: main universe/' /etc/apt/sources.list.d/ubuntu.sources \
  && apt-get -S "${UBUNTU_SNAPSHOT}" update \
  && ca_version="$(apt-cache policy ca-certificates | sed -n 's/ *Candidate: //p')" \
  && test -n "${ca_version}" && test "${ca_version}" != '(none)' \
  && apt-get -S "${UBUNTU_SNAPSHOT}" install -y --no-install-recommends --allow-downgrades \
  "ca-certificates=${ca_version}" nbdkit nbdkit-plugin-dev pkg-config \
  && apt-get -S "${UBUNTU_SNAPSHOT}" build-dep -y nbdkit \
  && mkdir -p /src /out \
  && cd /src \
  && apt-get -S "${UBUNTU_SNAPSHOT}" source nbdkit \
  && src_dir="$(find /src -mindepth 1 -maxdepth 1 -type d -name 'nbdkit-*' -print -quit)" \
  && test -n "${src_dir}" \
  && cd "${src_dir}" \
  && ./configure --disable-perl --disable-python --disable-ocaml --disable-rust \
  --disable-ruby --disable-tcl --disable-lua --disable-golang \
  && for part in common/include common/replacements common/utils common/bitmap plugins/vddk; do make -C "${part}"; done \
  && plugin_dir="$(pkg-config --variable=plugindir nbdkit)" \
  && install -D -m 0755 plugins/vddk/.libs/nbdkit-vddk-plugin.so "/out${plugin_dir}/nbdkit-vddk-plugin.so"

FROM ca-bootstrap AS runtime
ARG UBUNTU_SNAPSHOT
ARG AWS_CLI_VERSION
ARG GOOGLE_CLOUD_CLI_VERSION
ARG GOOGLE_CLOUD_CLI_SHA256
ENV DEBIAN_FRONTEND=noninteractive \
  LANG=C.UTF-8 \
  HOME=/tmp \
  PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  DIB7_VENV_BIN=/opt/venv/bin \
  ANSIBLE_COLLECTIONS_PATH=/opt/ansible/collections \
  LIBGUESTFS_PATH=/tmp/libguestfs-appliance \
  OVFTOOL_ACCEPT_LICENSE=1 \
  PIP_DISABLE_PIP_VERSION_CHECK=1 \
  PYTHONDONTWRITEBYTECODE=1

COPY web/aws-cli-signing-key.asc /tmp/aws-cli-signing-key.asc
COPY --chmod=0755 container-entrypoint.sh /usr/local/bin/migrate-v2v-entrypoint
COPY --from=govc-builder /out/ /
COPY --from=vddk-plugin-builder /out/ /
COPY requirements.txt requirements.yml requirements-azure-constraints.txt requirements-container-azure.txt /tmp/migrate-v2v/

# Archive the sparse raw appliance and discard supermin's duplicate cache.
RUN set -eux; \
  sed -i 's/^Components: main$/Components: main universe/' /etc/apt/sources.list.d/ubuntu.sources; \
  apt-get -S "${UBUNTU_SNAPSHOT}" update; \
  ca_version="$(apt-cache policy ca-certificates | sed -n 's/ *Candidate: //p')"; \
  test -n "${ca_version}" && test "${ca_version}" != '(none)'; \
  apt-get -S "${UBUNTU_SNAPSHOT}" install -y --no-install-recommends \
  --allow-downgrades "ca-certificates=${ca_version}" \
  bash coreutils curl dialog findutils gawk gnupg grep groff \
  libguestfs-tools less linux-image-virtual nbdkit openssh-client openssl procps python3 \
  python3-venv qemu-utils rhsrvany sed tar unzip util-linux virt-v2v xz-utils; \
  curl -fsSLo /tmp/google-cloud-cli.tar.gz \
  "https://storage.googleapis.com/cloud-sdk-release/google-cloud-cli-${GOOGLE_CLOUD_CLI_VERSION}-linux-x86_64.tar.gz"; \
  echo "${GOOGLE_CLOUD_CLI_SHA256}  /tmp/google-cloud-cli.tar.gz" | sha256sum --check --status; \
  mkdir -p /opt/google-cloud-cli; \
  tar --extract --gzip --file /tmp/google-cloud-cli.tar.gz --directory /opt/google-cloud-cli; \
  ln -s /opt/google-cloud-cli/google-cloud-sdk/bin/gcloud /usr/local/bin/gcloud; \
  rm /tmp/google-cloud-cli.tar.gz; \
  mkdir -m 0700 /tmp/aws-gnupg; \
  aws_key_fingerprint="$(gpg --homedir /tmp/aws-gnupg --show-keys --with-colons --fingerprint /tmp/aws-cli-signing-key.asc | awk -F: '$1 == "fpr" { print $10; exit }')"; \
  test "${aws_key_fingerprint}" = FB5DB77FD5C118B80511ADA8A6310ACC4672475C; \
  gpg --homedir /tmp/aws-gnupg --batch --import /tmp/aws-cli-signing-key.asc; \
  curl -fsSLo /tmp/awscliv2.zip "https://awscli.amazonaws.com/awscli-exe-linux-x86_64-${AWS_CLI_VERSION}.zip"; \
  curl -fsSLo /tmp/awscliv2.zip.sig "https://awscli.amazonaws.com/awscli-exe-linux-x86_64-${AWS_CLI_VERSION}.zip.sig"; \
  gpg --homedir /tmp/aws-gnupg --batch --verify /tmp/awscliv2.zip.sig /tmp/awscliv2.zip; \
  mkdir /tmp/aws-cli; \
  unzip -q /tmp/awscliv2.zip -d /tmp/aws-cli; \
  /tmp/aws-cli/aws/install --install-dir /usr/local/aws-cli --bin-dir /usr/local/bin; \
  rm -rf /tmp/aws-cli /tmp/aws-gnupg /tmp/awscliv2.zip /tmp/awscliv2.zip.sig /tmp/aws-cli-signing-key.asc; \
  apt-get purge -y gnupg unzip; \
  apt-get autoremove -y; \
  python3 -m venv /opt/venv; \
  /opt/venv/bin/python -m pip install --no-cache-dir --no-compile -r /tmp/migrate-v2v/requirements.txt; \
  mkdir -p /opt/ansible/collections; \
  /opt/venv/bin/ansible-galaxy collection install \
  -p /opt/ansible/collections -r /tmp/migrate-v2v/requirements.yml; \
  /opt/venv/bin/python -m pip install --no-cache-dir --no-compile \
  -r /tmp/migrate-v2v/requirements-container-azure.txt \
  -c /tmp/migrate-v2v/requirements-azure-constraints.txt; \
  mkdir -p /opt; \
  env -u LIBGUESTFS_PATH libguestfs-make-fixed-appliance /opt/libguestfs-appliance; \
  chmod -R a+rX /opt/libguestfs-appliance; \
  test -r /opt/libguestfs-appliance/kernel; \
  test -r /opt/libguestfs-appliance/initrd; \
  test -r /opt/libguestfs-appliance/root; \
  test -r /opt/libguestfs-appliance/README.fixed; \
  tar --sparse --create --xz --file /opt/libguestfs-appliance.tar.xz \
  -C /opt/libguestfs-appliance .; \
  rm -rf /opt/libguestfs-appliance /var/tmp/.guestfs-0; \
  apt-get purge -y linux-image-virtual; \
  apt-get autoremove -y; \
  aws --version | grep -F "aws-cli/${AWS_CLI_VERSION}"; \
  gcloud version | grep -F "Google Cloud SDK ${GOOGLE_CLOUD_CLI_VERSION}"; \
  govc version; \
  virt-v2v-in-place --version; \
  plugin_dir="$(nbdkit --dump-config | sed -n 's/^plugindir=//p')"; \
  test -n "${plugin_dir}"; \
  test -r "${plugin_dir}/nbdkit-vddk-plugin.so"; \
  if ldd "${plugin_dir}/nbdkit-vddk-plugin.so" | grep -q 'not found'; then \
  echo 'nbdkit VDDK plugin has unresolved shared-library dependencies' >&2; exit 1; \
  fi; \
  test -s /opt/libguestfs-appliance.tar.xz; \
  rm -rf /tmp/migrate-v2v /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/archives/partial/*

# gcloud and Ansible run during the root build with HOME=/tmp. Let the
# non-root runtime user create its own configuration and local temporary dirs.
RUN rm -rf /tmp/.config /tmp/.ansible

# The async wrapper honors HOME; async_status resolves the container user's
# passwd home. Pin both to the same writable cache directory.
ENV ANSIBLE_ASYNC_DIR=/tmp/.ansible_async

# Package the public project source and operational files into the runtime.
# The root .dockerignore keeps credentials, local-only scripts, runtime data,
# and development metadata out of the image.
COPY . /opt/migrate-v2v/

WORKDIR /opt/migrate-v2v
ENTRYPOINT ["/usr/local/bin/migrate-v2v-entrypoint"]
CMD ["python3", "web/app.py", "serve", "--host", "0.0.0.0", "--port", "8080"]
