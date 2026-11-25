/*
 * ver_0.2 A2: the shared policy state (SPEC §4, "Sharing the policy between plugins").
 *
 * This module is compiled into slurmctld itself: docker/register_plugins.sh copies it to
 * src/slurmctld/adaptive_policy/ and adds it to slurmctld's sources. So the process holds exactly
 * one copy, whichever plugins are loaded and in whatever order. priority/adaptive's controller
 * publishes the active policy here, and select/cons_tres_adaptive's placement scorer (A4) reads
 * it. The plugins reach these functions the way they reach the rest of slurmctld (job_list,
 * lock_slurmctld(), ...): slurmctld exports its symbols, and a plugin's references bind to them
 * when slurmctld loads the plugin.
 *
 * Plain C with pthreads and no Slurm headers, so plugin/tests/ can build it on its own.
 *
 * Until something is published (no adaptive.conf, or a stock priority plugin), adaptive_policy_get()
 * returns -1 and a reader should behave like stock Slurm.
 */
#ifndef _ADAPTIVE_POLICY_H
#define _ADAPTIVE_POLICY_H

#include <stdint.h>

#define ADAPTIVE_NAME_LEN	64

/* One policy, as one line of policies.conf defines it (SPEC §6, §13.1) */
typedef struct {
	char name[ADAPTIVE_NAME_LEN];
	uint32_t fairshare;	/* ordering weights, summing to 100,000 */
	uint32_t age;
	uint32_t jobsize;	/* 0 except in BASE */
	double spread;		/* placement weights: sum to 1, or both 0 for stock placement */
	double fit;
} adaptive_policy_t;

/* What a reader gets */
typedef struct {
	adaptive_policy_t policy;	/* the policy in force */
	uint64_t seq;			/* changes on every publish or clear; 0 before the first */
	int64_t since;			/* simulated time (Unix seconds) it took effect */
} adaptive_policy_state_t;

/* Writer (the controller in priority/adaptive): make policy the active one from `since` on. */
extern void adaptive_policy_publish(const adaptive_policy_t *policy, int64_t since);

/* Forget the published policy, so readers fall back to stock behaviour. */
extern void adaptive_policy_clear(void);

/* Reader: copy the active policy into *out. Returns 0, or -1 when none is published. */
extern int adaptive_policy_get(adaptive_policy_state_t *out);

/* Reader: the change counter, without the copy. A cached copy is current while this is unchanged. */
extern uint64_t adaptive_policy_seq(void);

/* Both placement weights 0: stock node selection (SPEC §5) */
static inline int adaptive_policy_stock_placement(const adaptive_policy_t *p)
{
	return (p->spread == 0.0) && (p->fit == 0.0);
}

/* Same ordering weights, so a switch between the two needs no priority recompute (SPEC §8) */
static inline int adaptive_policy_same_ordering(const adaptive_policy_t *a,
						const adaptive_policy_t *b)
{
	return (a->fairshare == b->fairshare) && (a->age == b->age) &&
	       (a->jobsize == b->jobsize);
}

#endif
