/*
 * ver_0.2: parser for adaptive.conf, policies.conf and schedule.conf (SPEC §13.1).
 *
 * Plain C with libc only, and no Slurm headers, so it builds inside the plugin and also
 * standalone for the unit tests in plugin/tests/. The rules mirror scripts/validate.py.
 * Every function returns 0 on success, or -1 with the first problem written to err.
 */
#ifndef _ADAPTIVE_CONF_H
#define _ADAPTIVE_CONF_H

#include <stddef.h>
#include <stdint.h>

#include "adaptive_policy.h"	/* adaptive_policy_t, ADAPTIVE_NAME_LEN (shared with slurmctld) */

#define ADAPTIVE_CONF_NAME	"adaptive.conf"
#define ADAPTIVE_TOTAL_ORDERING	100000	/* every policy's ordering weights sum to this (SPEC §5) */
#define ADAPTIVE_MAX_POLICIES	64
#define ADAPTIVE_MAX_WINDOWS	256
#define ADAPTIVE_PATH_LEN	1024
#define ADAPTIVE_ERR_LEN	1280

enum {
	ADAPTIVE_SIG_FAIRNESS,
	ADAPTIVE_SIG_BALANCE,
	ADAPTIVE_SIG_UTIL,
	ADAPTIVE_SIG_COUNT
};

/* policies.conf: one adaptive_policy_t per line */
typedef struct {
	int count;
	adaptive_policy_t policy[ADAPTIVE_MAX_POLICIES];
} adaptive_policies_t;

/* One line of schedule.conf. The window covers [start, end). */
typedef struct {
	int daily;		/* 1: Daily=HH:MM-HH:MM, start/end in seconds after 00:00 UTC */
	int64_t start;		/* 0: one-off, start/end in Unix seconds (UTC) */
	int64_t end;
	int policy;		/* index into adaptive_policies_t */
	int line;
} adaptive_window_t;

typedef struct {
	int count;
	adaptive_window_t window[ADAPTIVE_MAX_WINDOWS];
} adaptive_schedule_t;

/* adaptive.conf. File paths are resolved against the directory adaptive.conf is in. */
typedef struct {
	char policies_file[ADAPTIVE_PATH_LEN];
	char schedule_file[ADAPTIVE_PATH_LEN];
	char log_file[ADAPTIVE_PATH_LEN];
	char placement_log[ADAPTIVE_PATH_LEN];		/* "" when absent */
	uint32_t tmin_s;
	char default_policy[ADAPTIVE_NAME_LEN];
	char fixed_policy[ADAPTIVE_NAME_LEN];		/* "" when absent */
	char force_alternate[2][ADAPTIVE_NAME_LEN];	/* "" when absent */
	uint32_t force_period_s;			/* 0 when absent */
	double min_demand;
	double queue_pressure_min;
	double signal_enter[ADAPTIVE_SIG_COUNT];
	double signal_exit[ADAPTIVE_SIG_COUNT];
	/* where the policy names were given, for errors found once policies.conf is read */
	int default_line, fixed_line, force_line;
} adaptive_conf_t;

extern const char *const adaptive_signal_names[ADAPTIVE_SIG_COUNT];
extern const char *const adaptive_required_policies[];	/* NULL-terminated, the 11 of SPEC §6 */

/* Parse policies.conf text. src names the file in error messages. */
extern int adaptive_parse_policies(const char *text, const char *src,
				   adaptive_policies_t *out, char *err, size_t errlen);

/*
 * Parse adaptive.conf text. src is the file's path: relative paths inside it are resolved
 * against its directory. calc_period_s is PriorityCalcPeriod in seconds (TMin and ForcePeriod
 * must be whole multiples of it); 0 skips that check. Policy names are checked later, by
 * adaptive_load(), once policies.conf has been read.
 */
extern int adaptive_parse_conf(const char *text, const char *src, uint32_t calc_period_s,
			       adaptive_conf_t *out, char *err, size_t errlen);

extern const adaptive_policy_t *adaptive_find_policy(const adaptive_policies_t *pols,
						     const char *name);

/* Read a whole file into a malloc'd, NUL-terminated string (caller frees). */
extern int adaptive_read_file(const char *path, char **text, char *err, size_t errlen);

/*
 * Parse schedule.conf text against a policy set. src names the file in error messages.
 * Empty text (or comments only) gives no windows.
 */
extern int adaptive_parse_schedule(const char *text, const char *src,
				   const adaptive_policies_t *pols, adaptive_schedule_t *out,
				   char *err, size_t errlen);

/* Index of the window that covers simulated time now (Unix seconds), or -1 */
extern int adaptive_window_at(const adaptive_schedule_t *sched, int64_t now);

/* Index of a policy by name, or -1 */
extern int adaptive_policy_index(const adaptive_policies_t *pols, const char *name);

/*
 * Read and check adaptive.conf at conf_path plus the policies.conf and schedule.conf it points
 * to, and pick the policy in force at the start of the run: FixedPolicy if set (mode "FIXED"),
 * the first ForceAlternate policy if set (mode "FORCED"), otherwise DefaultPolicy (mode
 * "DEFAULT"). sched may be NULL to skip reading schedule.conf.
 */
extern int adaptive_load(const char *conf_path, uint32_t calc_period_s,
			 adaptive_conf_t *conf, adaptive_policies_t *pols,
			 adaptive_schedule_t *sched,
			 const adaptive_policy_t **active, const char **mode,
			 char *err, size_t errlen);

#endif
