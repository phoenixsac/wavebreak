# Wavebreak simulated edge device: Debian slim + systemd PID 1.
# Build from the repo root: docker build -f sim/device/Dockerfile -t wavebreak-device .
# Also the source of the Firecracker rootfs (docker export).
FROM debian:bookworm-slim

ARG NODE_EXPORTER_VERSION=1.9.1
ARG FLUENT_BIT_REPO=https://packages.fluentbit.io/debian/bookworm
ENV container=docker DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd systemd-sysv dbus python3 ca-certificates curl gnupg \
      procps iproute2 \
 && curl -fsSL https://packages.fluentbit.io/fluentbit.key | gpg --dearmor -o /usr/share/keyrings/fluentbit.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/fluentbit.gpg] ${FLUENT_BIT_REPO} bookworm main" \
      > /etc/apt/sources.list.d/fluent-bit.list \
 && apt-get update && apt-get install -y --no-install-recommends fluent-bit \
 && arch=$(dpkg --print-architecture) \
 && curl -fsSL "https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/node_exporter-${NODE_EXPORTER_VERSION}.linux-${arch}.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local/bin "node_exporter-${NODE_EXPORTER_VERSION}.linux-${arch}/node_exporter" \
 && apt-get purge -y gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
 # container-hostile units
 && systemctl mask systemd-udevd.service systemd-udevd-kernel.socket systemd-udevd-control.socket \
      getty.target console-getty.service systemd-firstboot.service systemd-remount-fs.service \
      dev-hugepages.mount sys-fs-fuse-connections.mount apt-daily.timer apt-daily-upgrade.timer \
      e2scrub_all.timer fstrim.timer \
 && rm -f /etc/machine-id && touch /etc/machine-id

COPY sim/device/files/ /
COPY sim/ota-agent/ota_agent/ /opt/ota-agent/ota_agent/
COPY sim/bundles/v1.0/ /opt/app/slot_a/

RUN ln -s slot_a /opt/app/current \
 && mkdir -p /var/lib/node_exporter/textfile /var/lib/ota-agent /var/lib/fluent-bit /var/log/wavebreak \
      /var/lib/wavebreak \
 && chmod +x /usr/local/lib/wavebreak/*.sh \
 && systemctl enable wavebreak-identity.service wavebreak-boot-record.service inference-app.service \
      ota-agent.service node_exporter.service fluent-bit.service wavebreak-restarts.timer

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
