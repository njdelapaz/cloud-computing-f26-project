/*
 * Load adaptive.conf (and the policies.conf it names) with the plugin's own parser, outside
 * slurmctld, and print what the plugin would pick. Used by plugin/tests/test_adaptive_conf.py.
 *
 * Usage: adaptive_conf_cli <adaptive.conf> [PriorityCalcPeriod seconds, default 60]
 * Prints "OK policy=<name> mode=<FIXED|DEFAULT> Fairshare=<n> Age=<n> JobSize=<n> policies=<n>"
 * and exits 0, or prints "ERROR <message>" and exits 1.
 */
#include <stdio.h>
#include <stdlib.h>

#include "adaptive_conf.h"

int main(int argc, char **argv)
{
	static adaptive_conf_t conf;
	static adaptive_policies_t pols;
	const adaptive_policy_t *active;
	const char *mode;
	char err[ADAPTIVE_ERR_LEN];
	unsigned long calc = 60;

	if ((argc < 2) || (argc > 3)) {
		fprintf(stderr, "usage: %s <adaptive.conf> [calc period s]\n", argv[0]);
		return 2;
	}
	if (argc == 3)
		calc = strtoul(argv[2], NULL, 10);

	if (adaptive_load(argv[1], (uint32_t) calc, &conf, &pols, &active, &mode,
			  err, sizeof(err))) {
		printf("ERROR %s\n", err);
		return 1;
	}
	printf("OK policy=%s mode=%s Fairshare=%u Age=%u JobSize=%u policies=%d\n",
	       active->name, mode, active->fairshare, active->age, active->jobsize, pols.count);
	return 0;
}
