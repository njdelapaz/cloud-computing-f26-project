# Lean image for the UB Slurm simulator v3.0 (P1).
# Build from ver_0.2/:   docker build -f docker/Dockerfile -t ver02/slurm-sim:vclock .
# Sources come from third_party/ (pinned commits recorded in PROGRESS.md):
#   third_party/slurm_simulator  ubccr-slurm-simulator/slurm_simulator @ f99a71e (branch slurm-23-02-sim)
#   third_party/slurm_sim_tools  ubccr-slurm-simulator/slurm_sim_tools @ 21ba58d (branch v3.0-branch)

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Build and run dependencies (same set the upstream image uses for Slurm, minus the desktop tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config ca-certificates openssl autoconf automake libtool \
        python3 python3-pip \
        munge libmunge-dev libjwt-dev libyaml-dev libdbus-1-dev libssl-dev \
        zstd libzstd-dev \
        libmariadb-dev mariadb-server mariadb-client \
    && rm -rf /var/lib/apt/lists/*

# Python packages the simulator's run tool (slurmsim) needs
RUN pip3 install --no-cache-dir numpy pandas pymysql psutil

# The simulator expects to run as user "slurm"; uid/gid 1000 matches the host user so mounted files stay owned by them
RUN groupadd -g 1000 slurm && useradd -m -u 1000 -g slurm -s /bin/bash slurm

# MariaDB account used by slurmdbd (local-only, simulator use)
RUN mkdir -p /run/mysqld && chown mysql:mysql /run/mysqld && \
    (mysqld_safe --skip-networking=0 --bind-address=127.0.0.1 &) && \
    for i in $(seq 1 30); do mysqladmin ping >/dev/null 2>&1 && break; sleep 1; done && \
    mysql -e "CREATE USER 'slurm'@'localhost' IDENTIFIED BY 'slurm';" && \
    mysql -e "GRANT ALL PRIVILEGES ON *.* TO 'slurm'@'localhost' WITH GRANT OPTION;" && \
    mysqladmin shutdown

# Simulator source, the ver_0.2 patches to it (docker/patches/, applied in name order; the
# virtual clock is off unless sim.conf sets ClockMode=virtual), and the plugin forks registered
# in its build system
COPY third_party/slurm_simulator /opt/src/slurm_simulator
COPY docker/patches /opt/patches
RUN cd /opt/src/slurm_simulator && for p in /opt/patches/*.patch; do patch -p1 --quiet < "$p"; done
COPY docker/register_plugins.sh /opt/register_plugins.sh
COPY docker/m4 /opt/m4
COPY plugin /opt/ver02_plugin
RUN bash /opt/register_plugins.sh /opt/src/slurm_simulator /opt/ver02_plugin

# Build the simulator (same configure flags as the upstream image's optimized build)
RUN mkdir -p /opt/bld/slurm_sim_opt && cd /opt/bld/slurm_sim_opt && \
    /opt/src/slurm_simulator/configure --prefix=/opt/slurm_sim \
        --disable-x11 --enable-front-end --with-hdf5=no --enable-simulator \
        CFLAGS='-O3 -Wno-error=unused-variable -Wno-error=implicit-function-declaration' \
        > /opt/bld/configure.log 2>&1 && \
    make -j"$(nproc)" > /opt/bld/make.log 2>&1 && \
    make install > /opt/bld/install.log 2>&1

# Simulator run tool, plus the extra Python packages it imports
COPY third_party/slurm_sim_tools /opt/slurm_sim_tools
RUN pip3 install --no-cache-dir tqdm py-cpuinfo zstandard
ENV PATH=/opt/slurm_sim/bin:/opt/slurm_sim/sbin:/opt/slurm_sim_tools/bin:$PATH \
    PYTHONPATH=/opt/slurm_sim_tools/src

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
