/*
 * ver_0.2 A2: the controller's decision step, decide(snapshot, config, now, controller_state)
 * (SPEC §4, §8, §13.1, §13.3).
 *
 * adaptive_decide() is a pure function: it reads its arguments, calls the sampler and the
 * classifier it is given, and writes only *out (the decision, including the controller state
 * after the tick). It uses no Slurm locks or globals, so plugin/tests/ runs it outside slurmctld.
 * priority/adaptive calls it once per PriorityCalcPeriod from its decay loop and applies the
 * decision (hot swap, decision log).
 *
 * Modes, for one tick at simulated time now:
 *   OVERRIDE  a schedule.conf window covers now: its policy, at once, even during a cooldown.
 *             No sampling. This holds in every mode: an admin's window always wins (SPEC §8).
 *   (window just ended) on the first tick after a window the controller goes straight back to
 *             the run's own policy (DefaultPolicy in AUTO, FixedPolicy in FIXED, the first
 *             ForceAlternate policy in FORCED), with no sampling, and a fresh cooldown starts.
 *   FIXED     FixedPolicy, always. Never eligible, never samples.
 *   FORCED    ForceAlternate=A,B: A first, then the other one every ForcePeriod. Never samples.
 *   AUTO      eligible when no switch has happened yet or TMin has passed since the last
 *             switch (or window end). Only then is the sampler called, and the classifier picks
 *             the policy. In cooldown nothing is sampled (G4).
 *
 * Time is counted in whole controller ticks: an interval counts as TMin (or ForcePeriod) once it
 * is within half a tick of it, so a tick that lands a second late doesn't push a switch back a
 * whole tick. TMin and ForcePeriod are whole multiples of PriorityCalcPeriod (SPEC §8, §13.1).
 */
#ifndef _ADAPTIVE_CTL_H
#define _ADAPTIVE_CTL_H

#include <stddef.h>
#include <stdint.h>

#include "adaptive_conf.h"

typedef enum {
	ADAPTIVE_MODE_AUTO,
	ADAPTIVE_MODE_OVERRIDE,
	ADAPTIVE_MODE_FIXED,
	ADAPTIVE_MODE_FORCED
} adaptive_mode_t;

extern const char *const adaptive_mode_names[];	/* "AUTO", "OVERRIDE", "FIXED", "FORCED" */

/* One state sample (SPEC §7). A3 fills it in; A2 has no sampler. */
typedef struct {
	double signal[ADAPTIVE_SIG_COUNT];	/* s_fairness, s_balance, s_util, each in [0, 1] */
	double queue_pressure;			/* >= 0 */
	double node_load_sd;			/* in [0, 0.5] */
} adaptive_snapshot_t;

/* Takes a snapshot. Returns 0 with *out filled, or -1 when it can't. */
typedef int (*adaptive_sample_fn)(void *ctx, adaptive_snapshot_t *out);

/* The "snapshot" argument of decide(): taken lazily, only on an eligible AUTO tick */
typedef struct {
	adaptive_sample_fn take;
	void *ctx;
} adaptive_sampler_t;

/* Classifier memory between samples (A3: which signals are active, for hysteresis) */
typedef struct {
	int active[ADAPTIVE_SIG_COUNT];
} adaptive_class_state_t;

struct adaptive_ctl_config;

/*
 * Picks the policy for a snapshot: returns an index into cfg->pols, and writes the classifier
 * memory after this sample to *after. Must not keep pointers to its arguments.
 */
typedef int (*adaptive_classify_fn)(void *ctx, const adaptive_snapshot_t *snap,
				    const struct adaptive_ctl_config *cfg,
				    const adaptive_class_state_t *before,
				    adaptive_class_state_t *after);

typedef struct adaptive_ctl_config {
	const adaptive_policies_t *pols;
	const adaptive_schedule_t *sched;	/* NULL or empty: no windows */
	uint32_t period_s;			/* PriorityCalcPeriod, the tick length */
	uint32_t tmin_s;
	int default_policy;			/* indexes into pols */
	int fixed_policy;			/* -1 when not FIXED */
	int force_policy[2];			/* -1 when not FORCED */
	uint32_t force_period_s;
	adaptive_classify_fn classify;		/* NULL: the A2 stub, which picks DefaultPolicy */
	void *classify_ctx;
} adaptive_ctl_config_t;

/* controller_state: what the controller remembers from one tick to the next */
typedef struct {
	int active;			/* index of the policy in force */
	int have_switch;		/* 0 until the first switch or window end */
	int64_t last_switch;		/* when the cooldown started (last switch or window end) */
	int in_window;			/* the previous tick was OVERRIDE */
	int64_t force_since;		/* FORCED: start of the current period; -1 before the first tick */
	adaptive_class_state_t cls;
} adaptive_ctl_state_t;

typedef struct {
	adaptive_mode_t mode;
	int window;			/* the covering window's index, or -1 */
	int eligible;			/* AUTO and out of cooldown */
	int sampled;			/* the sampler was called and gave a snapshot */
	adaptive_snapshot_t snap;	/* valid when sampled */
	int active;			/* policy in force before the tick */
	int chosen;			/* policy in force after the tick */
	int switched;			/* chosen != active */
	int reorder;			/* switched, and the ordering weights differ: recompute */
	adaptive_ctl_state_t next;	/* controller state after the tick */
} adaptive_decision_t;

/*
 * Fill *cfg from a loaded configuration. classify is NULL (the A2 stub). Returns 0, or -1 with
 * err set when a policy named in conf is missing from pols.
 */
extern int adaptive_ctl_config(const adaptive_conf_t *conf, const adaptive_policies_t *pols,
			       const adaptive_schedule_t *sched, uint32_t period_s,
			       adaptive_ctl_config_t *cfg, char *err, size_t errlen);

/* The run's own mode outside windows: FIXED, FORCED or AUTO */
extern adaptive_mode_t adaptive_ctl_base_mode(const adaptive_ctl_config_t *cfg);

/* The controller state at the start of a run: no switch yet, the run's first policy in force */
extern void adaptive_ctl_init(const adaptive_ctl_config_t *cfg, adaptive_ctl_state_t *st);

/* decide(snapshot, config, now, controller_state) -> decision. snapshot may be NULL (no sampler). */
extern void adaptive_decide(const adaptive_sampler_t *snapshot, const adaptive_ctl_config_t *cfg,
			    int64_t now, const adaptive_ctl_state_t *state,
			    adaptive_decision_t *out);

/* Decision log (SPEC §13.3) */
extern const char adaptive_decision_header[];	/* the header line, with its newline */

/*
 * One decision-log row, with its newline, into buf. active and chosen are the names of
 * d->active and d->chosen. swap_us < 0 leaves swap_us empty (it must be >= 0 exactly when
 * d->switched). Returns the length, or -1 if buf is too small.
 */
extern int adaptive_decision_row(const adaptive_decision_t *d, const char *active,
				 const char *chosen, int64_t now, int64_t swap_us,
				 uint32_t pending_jobs, char *buf, size_t len);

#endif
