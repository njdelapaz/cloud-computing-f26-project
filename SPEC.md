# Specification: [Project Name]

**Author(s):** [Names]
**Date:** [YYYY-MM-DD]
**Status:** [Draft / Under review / Approved]

---

## 1. Problem
Create a scheduler in Slurm that can change policies based on what it wants to maximize

## 2. Goals
- Create a test environment that simulates 24 GPU devices in Slurm across 3 nodes (8 GPUs each)
- Create a scheduler that handles different policies that prioritize different metrics
    - Utilization
    - Performance
    - Stability/Load Balance
    - Fairness
- Run simulated workloads using the University of Buffalo's Slurm simulator to evaluate different policies performance

## 3. Non-Goals
- Optimize job resource usage predictions
