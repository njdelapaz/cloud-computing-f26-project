# UB Slurm Simulator v3.0 with this project's patches applied.
#
# Adds a "switch_policy" event type to the simulator's events file, so a
# scheduling policy can be changed at a set simulated time (the simulator
# does not answer scontrol during a run). See sim/patches/switch_policy.patch.
#
# Build (from the repository root):
#   docker build -f docker/Dockerfile -t gpu24-slurm-sim:latest .
FROM nsimakov/slurm_sim:v3.0

COPY sim/patches/switch_policy.patch /tmp/switch_policy.patch

# The image ships the simulator source and configured build trees; rebuild
# the release build (/opt/slurm_sim) after patching.
RUN set -e; \
    cd /opt/slurm_sim_tools/slurm_simulator; \
    patch -p1 < /tmp/switch_policy.patch; \
    cd /opt/slurm_sim_bld/slurm_sim_opt; \
    make -j"$(nproc)" > /tmp/build.log 2>&1 || { tail -50 /tmp/build.log; exit 1; }; \
    make install > /tmp/install.log 2>&1 || { tail -50 /tmp/install.log; exit 1; }
