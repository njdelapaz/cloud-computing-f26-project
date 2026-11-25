/*
 * Run the controller's decide() (plugin/priority_adaptive/adaptive_ctl.c) outside slurmctld over
 * a scripted list of ticks, with the plugin's own parser loading adaptive.conf, policies.conf and
 * schedule.conf. Used by plugin/tests/test_decide.py.
 *
 * Usage: adaptive_ctl_cli [--stub] <adaptive.conf> <ticks file> [PriorityCalcPeriod s, default 60]
 *
 * Ticks file: one tick per line, '#' comments allowed:
 *   <sim_time> [want=<policy>] [nosample] [pending=<n>]
 *     want=P     what the test classifier answers if decide() calls it (default DefaultPolicy)
 *     nosample   the sampler can't take a snapshot on this tick
 *     pending=N  pending_jobs for the log row (default 0)
 * The test sampler returns s_fairness 0.5, s_balance 0.25, s_util 0.125, queue_pressure 1.5 and
 * node_load_sd 0.1, and counts its calls. With --stub there is no sampler and no classifier, as
 * in the A2 plugin.
 *
 * stdout: the decision log the plugin would write (SPEC §13.3), with swap_us 0 on switches.
 * stderr: one line per tick, "tick <sim_time> calls=<sampler calls> reorder=<0|1> window=<i>".
 * Exit 0, or 1 with "ERROR <message>" on stdout.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "adaptive_ctl.h"

typedef struct {
	int calls;	/* sampler calls on this tick */
	int nosample;
} sampler_ctx_t;

typedef struct {
	int want;	/* index, or -1 for DefaultPolicy */
} classify_ctx_t;

static int _sample(void *ctx, adaptive_snapshot_t *out)
{
	sampler_ctx_t *s = ctx;

	s->calls++;
	if (s->nosample)
		return -1;
	out->signal[ADAPTIVE_SIG_FAIRNESS] = 0.5;
	out->signal[ADAPTIVE_SIG_BALANCE] = 0.25;
	out->signal[ADAPTIVE_SIG_UTIL] = 0.125;
	out->queue_pressure = 1.5;
	out->node_load_sd = 0.1;
	return 0;
}

static int _classify(void *ctx, const adaptive_snapshot_t *snap,
		     const adaptive_ctl_config_t *cfg, const adaptive_class_state_t *before,
		     adaptive_class_state_t *after)
{
	const classify_ctx_t *c = ctx;

	(void) snap;
	*after = *before;
	return (c->want >= 0) ? c->want : cfg->default_policy;
}

int main(int argc, char **argv)
{
	static adaptive_conf_t conf;
	static adaptive_policies_t pols;
	static adaptive_schedule_t sched;
	adaptive_ctl_config_t cfg;
	adaptive_ctl_state_t st;
	sampler_ctx_t sctx;
	classify_ctx_t cctx;
	adaptive_sampler_t sampler = { _sample, &sctx };
	const adaptive_policy_t *first;
	const char *mode;
	char err[ADAPTIVE_ERR_LEN], line[512], row[512];
	unsigned long calc = 60;
	int stub = 0, a = 1;
	FILE *ticks;

	if ((argc > 1) && !strcmp(argv[1], "--stub")) {
		stub = 1;
		a++;
	}
	if ((argc - a < 2) || (argc - a > 3)) {
		fprintf(stderr, "usage: %s [--stub] <adaptive.conf> <ticks> [calc period s]\n",
			argv[0]);
		return 2;
	}
	if (argc - a == 3)
		calc = strtoul(argv[a + 2], NULL, 10);

	if (adaptive_load(argv[a], (uint32_t) calc, &conf, &pols, &sched, &first, &mode,
			  err, sizeof(err)) ||
	    adaptive_ctl_config(&conf, &pols, &sched, (uint32_t) calc, &cfg, err, sizeof(err))) {
		printf("ERROR %s\n", err);
		return 1;
	}
	if (!stub) {
		cfg.classify = _classify;
		cfg.classify_ctx = &cctx;
	}
	adaptive_ctl_init(&cfg, &st);
	if (strcmp(pols.policy[st.active].name, first->name)) {
		printf("ERROR adaptive_ctl_init picked %s but adaptive_load picked %s\n",
		       pols.policy[st.active].name, first->name);
		return 1;
	}
	if (!(ticks = fopen(argv[a + 1], "r"))) {
		printf("ERROR can't open %s\n", argv[a + 1]);
		return 1;
	}

	fputs(adaptive_decision_header, stdout);
	while (fgets(line, sizeof(line), ticks)) {
		adaptive_decision_t d;
		char *tok, *hash = strchr(line, '#');
		long long now;
		unsigned pending = 0;

		if (hash)
			*hash = '\0';
		if (!(tok = strtok(line, " \t\r\n")))
			continue;
		now = strtoll(tok, NULL, 10);
		memset(&sctx, 0, sizeof(sctx));
		cctx.want = -1;
		while ((tok = strtok(NULL, " \t\r\n"))) {
			if (!strncmp(tok, "want=", 5)) {
				if ((cctx.want = adaptive_policy_index(&pols, tok + 5)) < 0) {
					printf("ERROR unknown policy in %s\n", tok);
					return 1;
				}
			} else if (!strcmp(tok, "nosample")) {
				sctx.nosample = 1;
			} else if (!strncmp(tok, "pending=", 8)) {
				pending = (unsigned) strtoul(tok + 8, NULL, 10);
			} else {
				printf("ERROR unknown tick option %s\n", tok);
				return 1;
			}
		}

		adaptive_decide(stub ? NULL : &sampler, &cfg, now, &st, &d);
		if (adaptive_decision_row(&d, pols.policy[d.active].name,
					  pols.policy[d.chosen].name, now, d.switched ? 0 : -1,
					  pending, row, sizeof(row)) < 0) {
			printf("ERROR row too long\n");
			return 1;
		}
		fputs(row, stdout);
		fprintf(stderr, "tick %lld calls=%d reorder=%d window=%d\n",
			now, sctx.calls, d.reorder, d.window);
		st = d.next;
	}
	fclose(ticks);
	return 0;
}
