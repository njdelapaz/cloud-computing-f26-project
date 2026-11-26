# UB Slurm Simulator image for ver_0.3.
#
# Build from ver_0.3 (sim/Dockerfile.dockerignore keeps only the pinned
# third_party checkouts and sim/entrypoint.sh in the build context):
#   docker build -f sim/Dockerfile -t slurmsim:v03 .
#
# Lean on purpose: the upstream image also ships Jupyter, RStudio, Eclipse and
# Chrome, none of which the simulation needs.

FROM ubuntu:22.04

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential pkg-config git ca-certificates sudo procps \
        python3 python3-dev python3-pip \
        mariadb-server mariadb-client libmariadb-dev \
        munge libmunge-dev libyaml-dev libjwt-dev libdbus-1-dev \
        libjson-c-dev libssl-dev zstd libzstd-dev && \
    rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
        numpy==2.1.3 pandas==2.2.3 pyarrow==18.0.0 \
        PyMySQL==1.1.1 psutil==6.1.0 tqdm==4.67.0

# Simulation user. The runner requires SlurmUser to be the user it runs as.
RUN useradd -m -u 1000 -s /bin/bash slurm && \
    echo "slurm ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/slurm

# MariaDB accounts used by slurmdbd.
RUN mkdir -p /run/mysqld && chown mysql:mysql /run/mysqld && \
    (mysqld_safe --user=mysql >/dev/null 2>&1 &) && \
    for i in $(seq 1 30); do mysqladmin ping --silent && break; sleep 1; done && \
    mysql -e "CREATE USER 'slurm'@'localhost' IDENTIFIED BY 'slurm';" && \
    mysql -e "GRANT ALL PRIVILEGES ON *.* TO 'slurm'@'localhost' WITH GRANT OPTION;" && \
    mysqladmin shutdown

# Toolkit (slurmsim runner) and Slurm simulator source, from the pinned checkouts.
COPY --chown=slurm:slurm third_party/slurm_sim_tools /opt/slurm_sim_tools
COPY --chown=slurm:slurm third_party/slurm_simulator /opt/slurm_sim_src

# ver_0.3 policy hook (SPEC §7): periodic policy tick and in-place configuration switch.
# Inactive unless sim.conf sets PolicyDir (and PolicyTickSeconds/PolicyCommand for ticks).
COPY --chown=slurm:slurm sim/patches /opt/slurm_sim_patches
RUN cd /opt/slurm_sim_src && sudo -u slurm patch -p1 < /opt/slurm_sim_patches/policy_hook.patch

RUN mkdir -p /opt/slurm_sim_bld /opt/slurm_sim && \
    chown slurm:slurm /opt/slurm_sim_bld /opt/slurm_sim && \
    cd /opt/slurm_sim_bld && \
    sudo -u slurm /opt/slurm_sim_src/configure --prefix=/opt/slurm_sim \
        --disable-x11 --enable-front-end --with-hdf5=no --enable-simulator \
        CFLAGS='-O3 -Wno-error=unused-variable -Wno-error=implicit-function-declaration' \
        > /opt/slurm_sim_bld/configure.log 2>&1 && \
    { sudo -u slurm make -j"$(nproc)" > /opt/slurm_sim_bld/make.log 2>&1 || \
      { grep -n 'error' /opt/slurm_sim_bld/make.log | tail -40; exit 1; }; } && \
    sudo -u slurm make install > /opt/slurm_sim_bld/install.log 2>&1

ENV PATH="/opt/slurm_sim/bin:/opt/slurm_sim/sbin:/opt/slurm_sim_tools/bin:${PATH}" \
    PYTHONPATH="/opt/slurm_sim_tools/src"

COPY --chmod=755 sim/entrypoint.sh /usr/local/bin/entrypoint.sh

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
