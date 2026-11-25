/*
 * ver_0.2 A2: the controller's decision step. See adaptive_ctl.h for the rules.
 * Plain C with libc only, and no Slurm headers.
 */
#include <stdio.h>
#include <string.h>

#include "adaptive_ctl.h"

const char *const adaptive_mode_names[] = { "AUTO", "OVERRIDE", "FIXED", "FORCED" };

const char adaptive_decision_header[] =
	"sim_time,mode,eligible,sampled,s_fairness,s_balance,s_util,queue_pressure,"
	"node_load_sd,active_policy,chosen_policy,switched,swap_us,pending_jobs\n";

static int _fail(char *err, size_t errlen, const char *what, const char *name)
{
	if (err && errlen)
		snprintf(err, errlen, "%s: unknown policy '%s'", what, name);
	return -1;
}

extern int adaptive_ctl_config(const adaptive_conf_t *conf, const adaptive_policies_t *pols,
			       const adaptive_schedule_t *sched, uint32_t period_s,
			       adaptive_ctl_config_t *cfg, char *err, size_t errlen)
{
	int i;

	memset(cfg, 0, sizeof(*cfg));
	cfg->pols = pols;
	cfg->sched = sched;
	cfg->period_s = period_s;
	cfg->tmin_s = conf->tmin_s;
	cfg->fixed_policy = -1;
	cfg->force_policy[0] = cfg->force_policy[1] = -1;
	cfg->force_period_s = conf->force_period_s;

	if ((cfg->default_policy = adaptive_policy_index(pols, conf->default_policy)) < 0)
		return _fail(err, errlen, "DefaultPolicy", conf->default_policy);
	if (conf->fixed_policy[0] &&
	    ((cfg->fixed_policy = adaptive_policy_index(pols, conf->fixed_policy)) < 0))
		return _fail(err, errlen, "FixedPolicy", conf->fixed_policy);
	for (i = 0; conf->force_alternate[0][0] && (i < 2); i++) {
		if ((cfg->force_policy[i] =
		     adaptive_policy_index(pols, conf->force_alternate[i])) < 0)
			return _fail(err, errlen, "ForceAlternate", conf->force_alternate[i]);
	}
	return 0;
}

extern adaptive_mode_t adaptive_ctl_base_mode(const adaptive_ctl_config_t *cfg)
{
	if (cfg->fixed_policy >= 0)
		return ADAPTIVE_MODE_FIXED;
	if (cfg->force_policy[0] >= 0)
		return ADAPTIVE_MODE_FORCED;
	return ADAPTIVE_MODE_AUTO;
}

/* The policy the run returns to outside windows */
static int _base_policy(const adaptive_ctl_config_t *cfg)
{
	switch (adaptive_ctl_base_mode(cfg)) {
	case ADAPTIVE_MODE_FIXED:
		return cfg->fixed_policy;
	case ADAPTIVE_MODE_FORCED:
		return cfg->force_policy[0];
	default:
		return cfg->default_policy;
	}
}

extern void adaptive_ctl_init(const adaptive_ctl_config_t *cfg, adaptive_ctl_state_t *st)
{
	memset(st, 0, sizeof(*st));
	st->active = _base_policy(cfg);
	st->force_since = -1;
}

/*
 * Has span (a whole number of ticks) passed between since and now? Counted in whole ticks, so
 * an interval within half a tick of span counts as span.
 */
static int _passed(int64_t now, int64_t since, uint32_t span, uint32_t period)
{
	int64_t slack = period / 2;

	return (now - since + slack) >= (int64_t) span;
}

extern void adaptive_decide(const adaptive_sampler_t *snapshot, const adaptive_ctl_config_t *cfg,
			    int64_t now, const adaptive_ctl_state_t *state,
			    adaptive_decision_t *out)
{
	adaptive_ctl_state_t next = *state;
	adaptive_mode_t base = adaptive_ctl_base_mode(cfg);
	int chosen = state->active;
	int window_end = 0;

	memset(out, 0, sizeof(*out));
	out->window = cfg->sched ? adaptive_window_at(cfg->sched, now) : -1;

	if (out->window >= 0) {
		/* An admin's window takes over at once, cooldown or not, and nothing is sampled */
		out->mode = ADAPTIVE_MODE_OVERRIDE;
		chosen = cfg->sched->window[out->window].policy;
		next.in_window = 1;
	} else if (state->in_window) {
		/* First tick after a window: back to the run's own policy and a fresh cooldown */
		out->mode = base;
		chosen = _base_policy(cfg);
		next.in_window = 0;
		next.force_since = now;
		window_end = 1;
	} else if (base == ADAPTIVE_MODE_FIXED) {
		out->mode = base;
		chosen = cfg->fixed_policy;
	} else if (base == ADAPTIVE_MODE_FORCED) {
		const int *pair = cfg->force_policy;

		out->mode = base;
		if ((state->active != pair[0]) && (state->active != pair[1])) {
			chosen = pair[0];
			next.force_since = now;
		} else if (state->force_since < 0) {
			next.force_since = now;	/* the first tick starts the first period */
		} else if (_passed(now, state->force_since, cfg->force_period_s, cfg->period_s)) {
			chosen = (state->active == pair[0]) ? pair[1] : pair[0];
			next.force_since = now;
		}
	} else {
		out->mode = ADAPTIVE_MODE_AUTO;
		out->eligible = !state->have_switch ||
			_passed(now, state->last_switch, cfg->tmin_s, cfg->period_s);
		/* Sample only when a switch is allowed (G4). No snapshot, no switch. */
		if (out->eligible && snapshot && snapshot->take &&
		    !snapshot->take(snapshot->ctx, &out->snap)) {
			out->sampled = 1;
			if (cfg->classify)
				chosen = cfg->classify(cfg->classify_ctx, &out->snap, cfg,
						       &state->cls, &next.cls);
			else
				chosen = cfg->default_policy;	/* A2 stub; A3 classifies */
			if ((chosen < 0) || (chosen >= cfg->pols->count))
				chosen = state->active;
		}
	}

	out->active = state->active;
	out->chosen = chosen;
	out->switched = (chosen != state->active);
	out->reorder = out->switched &&
		!adaptive_policy_same_ordering(&cfg->pols->policy[state->active],
					       &cfg->pols->policy[chosen]);
	next.active = chosen;
	if (out->switched || window_end) {
		next.have_switch = 1;
		next.last_switch = now;
	}
	out->next = next;
}

/* A number for a signal column, or nothing when not sampled */
static const char *_num(char *buf, size_t len, int sampled, double x)
{
	if (!sampled)
		return "";
	snprintf(buf, len, "%.6f", x);
	return buf;
}

extern int adaptive_decision_row(const adaptive_decision_t *d, const char *active,
				 const char *chosen, int64_t now, int64_t swap_us,
				 uint32_t pending_jobs, char *buf, size_t len)
{
	char f[5][32], swap[32] = "";
	int n;

	if (swap_us >= 0)
		snprintf(swap, sizeof(swap), "%lld", (long long) swap_us);
	n = snprintf(buf, len, "%lld,%s,%d,%d,%s,%s,%s,%s,%s,%s,%s,%d,%s,%u\n",
		     (long long) now, adaptive_mode_names[d->mode], d->eligible, d->sampled,
		     _num(f[0], sizeof(f[0]), d->sampled, d->snap.signal[ADAPTIVE_SIG_FAIRNESS]),
		     _num(f[1], sizeof(f[1]), d->sampled, d->snap.signal[ADAPTIVE_SIG_BALANCE]),
		     _num(f[2], sizeof(f[2]), d->sampled, d->snap.signal[ADAPTIVE_SIG_UTIL]),
		     _num(f[3], sizeof(f[3]), d->sampled, d->snap.queue_pressure),
		     _num(f[4], sizeof(f[4]), d->sampled, d->snap.node_load_sd),
		     active, chosen, d->switched, swap, pending_jobs);
	return ((n < 0) || ((size_t) n >= len)) ? -1 : n;
}
