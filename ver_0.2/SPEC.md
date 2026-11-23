# Specification: [Project Name]

## DO NOT
Look at code outside this folder: ver_0.2

---

## 1. Problem
Create a scheduler in Slurm that can change policies based on what it wants to maximize

## 2. Goals
1) Update Slurm using the job priority plugin so that it can update pre-defined priority weight policies based on the state of the system
2) Create a mechanism that can schedule a specific policy for a specific time window. If nothing is scheduled, the system falls back to picking its own policy based on system state
    - Two modes: auto, override
3) Create the weight update mechanism
    - measure the overhead of changing weights
    - determine how often a system can change weights, allowing it to respond to system changes but causing minimal disruption to performance. Create a minimum wait time to change policies that balances system adaptibility and minimal system disruption.
4) Define system states that cause the weights to be updated
    - do not check system state for if the system is unable to change weights because of the minimum wait time
5) Create the weight combinations that act as policies
    - a set of policies that favor single metrics: fairness, balance, device utilization
    - a set of policies that combine these metrics, weighing some over others: an example: fairness > balance > utilization
6) Create simulated workloads
    - general traffic
    - specific traffic patterns that would trigger the weight changes
    - traffic patterns that are heavier during a specific 5 hour window
7) Run simulated workloads using the University of Buffalo's Slurm simulator to evaluate different policies performance

## 3. Non-Goals
- Do not try to predict job resources for better utilization. Only use what the requests have.



