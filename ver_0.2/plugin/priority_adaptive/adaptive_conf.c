/*
 * ver_0.2: parser for adaptive.conf and policies.conf (SPEC §13.1). See adaptive_conf.h.
 *
 * Grammar shared by both files: one entry per line, '#' starts a comment, blank lines are
 * ignored, and Key=Value tokens are separated by spaces. The checks follow scripts/validate.py,
 * except that this parser stops at the first problem and also rejects inf and nan.
 */
#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "adaptive_conf.h"

#define LINE_LEN	4096	/* longest line accepted, in bytes */
#define MAX_TOKENS	32	/* most Key=Value tokens on one line */
#define MAX_FILE	(1 << 20)

const char *const adaptive_signal_names[ADAPTIVE_SIG_COUNT] = {
	"fairness", "balance", "util"
};

const char *const adaptive_required_policies[] = {
	"BASE", "F", "B", "U", "F>B>U", "F>U>B", "B>F>U", "B>U>F", "U>F>B", "U>B>F", "EQ",
	NULL
};

typedef struct {
	char *key;
	char *val;
} kv_t;

typedef struct {
	const char *p;		/* rest of the text */
	int n;			/* line number of buf */
	char buf[LINE_LEN];
} line_iter_t;

static int _fail(char *err, size_t errlen, const char *fmt, ...)
{
	va_list ap;

	if (err && errlen) {
		va_start(ap, fmt);
		vsnprintf(err, errlen, fmt, ap);
		va_end(ap);
	}
	return -1;
}

/*
 * Move to the next line that isn't blank once its comment is removed. Leaves it, trimmed, in
 * it->buf. Returns 1 for a line, 0 at the end of the text, -1 for a line that is too long.
 */
static int _next_line(line_iter_t *it)
{
	while (*it->p) {
		const char *end = strchr(it->p, '\n');
		size_t len = end ? (size_t) (end - it->p) : strlen(it->p);
		char *s, *e;

		it->n++;
		if (len >= LINE_LEN)
			return -1;
		memcpy(it->buf, it->p, len);
		it->buf[len] = '\0';
		it->p += end ? len + 1 : len;

		if ((s = strchr(it->buf, '#')))
			*s = '\0';
		for (s = it->buf; *s && isspace((unsigned char) *s); s++)
			;
		e = s + strlen(s);
		while ((e > s) && isspace((unsigned char) e[-1]))
			*--e = '\0';
		if (*s) {
			memmove(it->buf, s, strlen(s) + 1);
			return 1;
		}
	}
	return 0;
}

/* Split a line into Key=Value tokens (in place). Returns the token count, or -1. */
static int _tokens(char *line, kv_t *kv, const char *src, int n, char *err, size_t errlen)
{
	int cnt = 0, i;
	char *s = line;

	while (*s) {
		char *tok, *eq;

		while (*s && isspace((unsigned char) *s))
			s++;
		if (!*s)
			break;
		tok = s;
		while (*s && !isspace((unsigned char) *s))
			s++;
		if (*s)
			*s++ = '\0';

		if (!(eq = strchr(tok, '=')) || (eq == tok))
			return _fail(err, errlen, "%s line %d: expected Key=Value, got '%s'",
				     src, n, tok);
		*eq = '\0';
		if (cnt == MAX_TOKENS)
			return _fail(err, errlen, "%s line %d: more than %d entries",
				     src, n, MAX_TOKENS);
		for (i = 0; i < cnt; i++) {
			if (!strcmp(kv[i].key, tok))
				return _fail(err, errlen, "%s line %d: key '%s' given twice",
					     src, n, tok);
		}
		kv[cnt].key = tok;
		kv[cnt].val = eq + 1;
		cnt++;
	}
	return cnt;
}

static const char *_get(kv_t *kv, int cnt, const char *key)
{
	int i;

	for (i = 0; i < cnt; i++) {
		if (!strcmp(kv[i].key, key))
			return kv[i].val;
	}
	return NULL;
}

/* A whole number >= 0 written in decimal digits only. */
static int _uint(const char *v, uint32_t *out)
{
	unsigned long long x = 0;
	const char *c;

	if (!*v)
		return -1;
	for (c = v; *c; c++) {
		if (!isdigit((unsigned char) *c))
			return -1;
		x = x * 10 + (unsigned) (*c - '0');
		if (x > 0xffffffffULL)
			return -1;
	}
	*out = (uint32_t) x;
	return 0;
}

/* A finite number in [lo, hi]; hi < lo means no upper bound. */
static int _num(const char *v, double lo, double hi, double *out)
{
	char *end;
	double x;

	if (!*v)
		return -1;
	errno = 0;
	x = strtod(v, &end);
	if (*end || (errno == ERANGE) || !isfinite(x) || (x < lo) || ((hi >= lo) && (x > hi)))
		return -1;
	*out = x;
	return 0;
}

/* A duration such as 90s, 15min or 2h, in seconds. */
static int _duration(const char *v, uint32_t *out)
{
	unsigned long long x = 0, mult;
	const char *c = v;

	if (!isdigit((unsigned char) *c))
		return -1;
	for (; isdigit((unsigned char) *c); c++) {
		x = x * 10 + (unsigned) (*c - '0');
		if (x > 0xffffffffULL)
			return -1;
	}
	if (!strcmp(c, "s"))
		mult = 1;
	else if (!strcmp(c, "min"))
		mult = 60;
	else if (!strcmp(c, "h"))
		mult = 3600;
	else
		return -1;
	if (x * mult > 0xffffffffULL)
		return -1;
	*out = (uint32_t) (x * mult);
	return 0;
}

/* Policy names use letters, digits, '_', '-' and '>'. */
static int _valid_name(const char *name)
{
	const char *c;

	if (!*name || (strlen(name) >= ADAPTIVE_NAME_LEN))
		return 0;
	for (c = name; *c; c++) {
		if (!isalnum((unsigned char) *c) && (*c != '_') && (*c != '-') && (*c != '>'))
			return 0;
	}
	return 1;
}

/* Resolve val against the directory that src is in, unless val is absolute. */
static int _resolve(const char *src, const char *val, char *out, size_t outlen)
{
	const char *slash = strrchr(src, '/');
	int len;

	if ((val[0] == '/') || !slash)
		len = snprintf(out, outlen, "%s", val);
	else
		len = snprintf(out, outlen, "%.*s/%s", (int) (slash - src), src, val);
	return ((len < 0) || ((size_t) len >= outlen)) ? -1 : 0;
}

extern const adaptive_policy_t *adaptive_find_policy(const adaptive_policies_t *pols,
						     const char *name)
{
	int i;

	for (i = 0; i < pols->count; i++) {
		if (!strcmp(pols->policy[i].name, name))
			return &pols->policy[i];
	}
	return NULL;
}

/* ---------------------------------------------------------------- policies.conf */

extern int adaptive_parse_policies(const char *text, const char *src,
				   adaptive_policies_t *out, char *err, size_t errlen)
{
	static const char *const keys[] = { "Fairshare", "Age", "JobSize", "Spread", "Fit" };
	line_iter_t *it;
	kv_t kv[MAX_TOKENS];
	int rc = -1, more, cnt, i, k;
	size_t used;

	memset(out, 0, sizeof(*out));
	if (!(it = calloc(1, sizeof(*it))))
		return _fail(err, errlen, "%s: out of memory", src);
	it->p = text;

	while ((more = _next_line(it)) > 0) {
		const char *name = NULL, *val[5] = { NULL };
		adaptive_policy_t *p;
		unsigned long long sum;
		double placement;
		int n = it->n;

		if ((cnt = _tokens(it->buf, kv, src, n, err, errlen)) < 0)
			goto done;
		for (i = 0; i < cnt; i++) {
			if (!strcmp(kv[i].key, "PolicyName")) {
				name = kv[i].val;
				continue;
			}
			for (k = 0; (k < 5) && strcmp(kv[i].key, keys[k]); k++)
				;
			if (k == 5) {
				_fail(err, errlen, "%s line %d: unknown key '%s' (use PolicyName, Fairshare, Age, JobSize, Spread and Fit)",
				      src, n, kv[i].key);
				goto done;
			}
			val[k] = kv[i].val;
		}
		if (!name) {
			_fail(err, errlen, "%s line %d: must start with PolicyName=", src, n);
			goto done;
		}
		if (!_valid_name(name)) {
			_fail(err, errlen, "%s line %d: bad policy name '%s'", src, n, name);
			goto done;
		}
		if (adaptive_find_policy(out, name)) {
			_fail(err, errlen, "%s line %d: policy '%s' defined twice", src, n, name);
			goto done;
		}
		for (k = 0; k < 5; k++) {
			if (!val[k]) {
				_fail(err, errlen, "%s line %d: %s: missing %s=", src, n, name, keys[k]);
				goto done;
			}
		}
		if (out->count == ADAPTIVE_MAX_POLICIES) {
			_fail(err, errlen, "%s line %d: more than %d policies",
			      src, n, ADAPTIVE_MAX_POLICIES);
			goto done;
		}

		p = &out->policy[out->count];
		strcpy(p->name, name);	/* length checked by _valid_name() */
		if (_uint(val[0], &p->fairshare) || _uint(val[1], &p->age) ||
		    _uint(val[2], &p->jobsize)) {
			_fail(err, errlen, "%s line %d: %s: Fairshare, Age and JobSize must be whole numbers >= 0 (got %s, %s, %s)",
			      src, n, name, val[0], val[1], val[2]);
			goto done;
		}
		if (_num(val[3], 0.0, 1.0, &p->spread) || _num(val[4], 0.0, 1.0, &p->fit)) {
			_fail(err, errlen, "%s line %d: %s: Spread and Fit must be numbers in [0, 1] (got %s, %s)",
			      src, n, name, val[3], val[4]);
			goto done;
		}
		sum = (unsigned long long) p->fairshare + p->age + p->jobsize;
		if (sum != ADAPTIVE_TOTAL_ORDERING) {
			_fail(err, errlen, "%s line %d: %s: Fairshare+Age+JobSize = %llu, must be %d",
			      src, n, name, sum, ADAPTIVE_TOTAL_ORDERING);
			goto done;
		}
		if (strcmp(name, "BASE") && p->jobsize) {
			_fail(err, errlen, "%s line %d: %s: JobSize must be 0 (only BASE may use it)",
			      src, n, name);
			goto done;
		}
		placement = p->spread + p->fit;
		if (!((fabs(placement - 1.0) <= 1e-6) || ((p->spread == 0.0) && (p->fit == 0.0)))) {
			_fail(err, errlen, "%s line %d: %s: Spread+Fit = %g, must be 1 (or both 0 for stock placement)",
			      src, n, name, placement);
			goto done;
		}
		if (!strcmp(name, "BASE") && (placement != 0.0)) {
			_fail(err, errlen, "%s line %d: BASE must use stock placement (Spread=Fit=0)",
			      src, n);
			goto done;
		}
		out->count++;
	}
	if (more < 0) {
		_fail(err, errlen, "%s line %d: longer than %d bytes", src, it->n, LINE_LEN - 1);
		goto done;
	}

	used = (size_t) snprintf(err, errlen, "%s: missing policies:", src);
	for (k = 0; adaptive_required_policies[k]; k++) {
		if (!adaptive_find_policy(out, adaptive_required_policies[k])) {
			if (used < errlen)
				used += (size_t) snprintf(err + used, errlen - used, " %s",
							  adaptive_required_policies[k]);
			rc = -2;
		}
	}
	if (rc == -2) {
		rc = -1;
		goto done;
	}
	if (errlen)
		err[0] = '\0';
	rc = 0;
done:
	free(it);
	return rc;
}

/* ---------------------------------------------------------------- adaptive.conf */

enum {
	K_POLICIES, K_SCHEDULE, K_TMIN, K_DEFAULT, K_MINDEMAND, K_QPMIN, K_LOG,
	K_PLACEMENT, K_FIXED, K_FORCE, K_FORCEPERIOD, K_COUNT
};
static const char *const conf_keys[K_COUNT] = {
	"PoliciesFile", "ScheduleFile", "TMin", "DefaultPolicy", "MinDemand",
	"QueuePressureMin", "LogFile", "PlacementLog", "FixedPolicy", "ForceAlternate",
	"ForcePeriod"
};
static const int conf_required[K_COUNT] = { 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0 };

/* One single-key line of adaptive.conf. */
static int _conf_value(adaptive_conf_t *out, int k, const char *v, const char *src, int n,
		       uint32_t calc_period_s, char *err, size_t errlen)
{
	char *path = NULL, *name = NULL;
	uint32_t secs;
	const char *comma;

	switch (k) {
	case K_POLICIES:	path = out->policies_file; break;
	case K_SCHEDULE:	path = out->schedule_file; break;
	case K_LOG:		path = out->log_file; break;
	case K_PLACEMENT:	path = out->placement_log; break;
	case K_DEFAULT:		name = out->default_policy; out->default_line = n; break;
	case K_FIXED:		name = out->fixed_policy; out->fixed_line = n; break;
	default:		break;
	}

	if (path) {
		if (!*v)
			return _fail(err, errlen, "%s line %d: %s is empty", src, n, conf_keys[k]);
		if (_resolve(src, v, path, ADAPTIVE_PATH_LEN))
			return _fail(err, errlen, "%s line %d: %s path is too long",
				     src, n, conf_keys[k]);
		return 0;
	}
	if (name) {
		if (!_valid_name(v))
			return _fail(err, errlen, "%s line %d: %s: unknown policy '%s'",
				     src, n, conf_keys[k], v);
		strcpy(name, v);
		return 0;
	}

	switch (k) {
	case K_TMIN:
	case K_FORCEPERIOD:
		if (_duration(v, &secs) || !secs)
			return _fail(err, errlen, "%s line %d: %s must look like 90s, 15min or 2h (got '%s')",
				     src, n, conf_keys[k], v);
		if (calc_period_s && (secs % calc_period_s))
			return _fail(err, errlen, "%s line %d: %s (%u s) must be a whole multiple of PriorityCalcPeriod (%u s)",
				     src, n, conf_keys[k], secs, calc_period_s);
		if (k == K_TMIN)
			out->tmin_s = secs;
		else
			out->force_period_s = secs;
		return 0;
	case K_MINDEMAND:
		if (_num(v, 0.0, 1.0, &out->min_demand))
			return _fail(err, errlen, "%s line %d: MinDemand must be a number in [0, 1] (got '%s')",
				     src, n, v);
		return 0;
	case K_QPMIN:
		if (_num(v, 0.0, -1.0, &out->queue_pressure_min))
			return _fail(err, errlen, "%s line %d: QueuePressureMin must be a number >= 0 (got '%s')",
				     src, n, v);
		return 0;
	case K_FORCE:
		out->force_line = n;
		comma = strchr(v, ',');
		if (!comma || strchr(comma + 1, ',') ||
		    ((size_t) (comma - v) >= ADAPTIVE_NAME_LEN) ||
		    (strlen(comma + 1) >= ADAPTIVE_NAME_LEN))
			return _fail(err, errlen, "%s line %d: ForceAlternate needs two different known policies, e.g. F,U (got '%s')",
				     src, n, v);
		memcpy(out->force_alternate[0], v, (size_t) (comma - v));
		out->force_alternate[0][comma - v] = '\0';
		strcpy(out->force_alternate[1], comma + 1);
		if (!_valid_name(out->force_alternate[0]) ||
		    !_valid_name(out->force_alternate[1]) ||
		    !strcmp(out->force_alternate[0], out->force_alternate[1]))
			return _fail(err, errlen, "%s line %d: ForceAlternate needs two different known policies, e.g. F,U (got '%s')",
				     src, n, v);
		return 0;
	default:
		return _fail(err, errlen, "%s line %d: internal error: key %d", src, n, k);
	}
}

extern int adaptive_parse_conf(const char *text, const char *src, uint32_t calc_period_s,
			       adaptive_conf_t *out, char *err, size_t errlen)
{
	line_iter_t *it;
	kv_t kv[MAX_TOKENS];
	int line[K_COUNT] = { 0 }, sig_line[ADAPTIVE_SIG_COUNT] = { 0 };
	int rc = -1, more, cnt, k, s;

	memset(out, 0, sizeof(*out));
	if (!(it = calloc(1, sizeof(*it))))
		return _fail(err, errlen, "%s: out of memory", src);
	it->p = text;

	while ((more = _next_line(it)) > 0) {
		const char *name, *enter, *exit_;
		int n = it->n;

		if ((cnt = _tokens(it->buf, kv, src, n, err, errlen)) < 0)
			goto done;

		if ((name = _get(kv, cnt, "Signal"))) {
			enter = _get(kv, cnt, "Enter");
			exit_ = _get(kv, cnt, "Exit");
			if ((cnt != 3) || !enter || !exit_) {
				_fail(err, errlen, "%s line %d: Signal lines need exactly Signal=, Enter= and Exit=",
				      src, n);
				goto done;
			}
			for (s = 0; (s < ADAPTIVE_SIG_COUNT) &&
				    strcmp(name, adaptive_signal_names[s]); s++)
				;
			if (s == ADAPTIVE_SIG_COUNT) {
				_fail(err, errlen, "%s line %d: unknown signal '%s' (use fairness, balance or util)",
				      src, n, name);
				goto done;
			}
			if (sig_line[s]) {
				_fail(err, errlen, "%s line %d: signal '%s' given twice", src, n, name);
				goto done;
			}
			if (_num(enter, 0.0, 1.0, &out->signal_enter[s]) ||
			    _num(exit_, 0.0, 1.0, &out->signal_exit[s])) {
				_fail(err, errlen, "%s line %d: %s: Enter and Exit must be numbers in [0, 1] (got %s, %s)",
				      src, n, name, enter, exit_);
				goto done;
			}
			if (!(out->signal_exit[s] < out->signal_enter[s])) {
				_fail(err, errlen, "%s line %d: %s: Exit must be below Enter",
				      src, n, name);
				goto done;
			}
			sig_line[s] = n;
			continue;
		}

		if (cnt != 1) {
			_fail(err, errlen, "%s line %d: one Key=Value per line (except Signal lines)",
			      src, n);
			goto done;
		}
		for (k = 0; (k < K_COUNT) && strcmp(kv[0].key, conf_keys[k]); k++)
			;
		if (k == K_COUNT) {
			_fail(err, errlen, "%s line %d: unknown key '%s'", src, n, kv[0].key);
			goto done;
		}
		if (line[k]) {
			_fail(err, errlen, "%s line %d: '%s' given twice", src, n, conf_keys[k]);
			goto done;
		}
		line[k] = n;
		if (_conf_value(out, k, kv[0].val, src, n, calc_period_s, err, errlen))
			goto done;
	}
	if (more < 0) {
		_fail(err, errlen, "%s line %d: longer than %d bytes", src, it->n, LINE_LEN - 1);
		goto done;
	}

	for (k = 0; k < K_COUNT; k++) {
		if (conf_required[k] && !line[k]) {
			_fail(err, errlen, "%s: missing %s", src, conf_keys[k]);
			goto done;
		}
	}
	for (s = 0; s < ADAPTIVE_SIG_COUNT; s++) {
		if (!sig_line[s]) {
			_fail(err, errlen, "%s: missing Signal=%s", src, adaptive_signal_names[s]);
			goto done;
		}
	}
	if (line[K_FORCE] && !line[K_FORCEPERIOD]) {
		_fail(err, errlen, "%s line %d: ForceAlternate needs ForcePeriod", src, line[K_FORCE]);
		goto done;
	}
	if (line[K_FORCEPERIOD] && !line[K_FORCE]) {
		_fail(err, errlen, "%s line %d: ForcePeriod without ForceAlternate",
		      src, line[K_FORCEPERIOD]);
		goto done;
	}
	if (line[K_FIXED] && line[K_FORCE]) {
		_fail(err, errlen, "%s: FixedPolicy and ForceAlternate can't both be set", src);
		goto done;
	}
	if (errlen)
		err[0] = '\0';
	rc = 0;
done:
	free(it);
	return rc;
}

/* ---------------------------------------------------------------- files */

extern int adaptive_read_file(const char *path, char **text, char *err, size_t errlen)
{
	FILE *f;
	size_t cap = 4096, len = 0, got;
	char *buf, *bigger;

	*text = NULL;
	if (!(f = fopen(path, "r")))
		return _fail(err, errlen, "can't open %s: %s", path, strerror(errno));
	if (!(buf = malloc(cap))) {
		fclose(f);
		return _fail(err, errlen, "%s: out of memory", path);
	}
	while ((got = fread(buf + len, 1, cap - len - 1, f)) > 0) {
		len += got;
		if (len < cap - 1)
			continue;
		if (cap >= MAX_FILE) {
			free(buf);
			fclose(f);
			return _fail(err, errlen, "%s is larger than %d bytes", path, MAX_FILE);
		}
		cap *= 2;
		if (!(bigger = realloc(buf, cap))) {
			free(buf);
			fclose(f);
			return _fail(err, errlen, "%s: out of memory", path);
		}
		buf = bigger;
	}
	if (ferror(f)) {
		free(buf);
		fclose(f);
		return _fail(err, errlen, "can't read %s", path);
	}
	fclose(f);
	buf[len] = '\0';
	if (strlen(buf) != len) {
		free(buf);
		return _fail(err, errlen, "%s contains a NUL byte", path);
	}
	*text = buf;
	return 0;
}

extern int adaptive_load(const char *conf_path, uint32_t calc_period_s,
			 adaptive_conf_t *conf, adaptive_policies_t *pols,
			 const adaptive_policy_t **active, const char **mode,
			 char *err, size_t errlen)
{
	char *text = NULL, why[ADAPTIVE_ERR_LEN];
	int rc, i;

	*active = NULL;
	*mode = NULL;
	if (adaptive_read_file(conf_path, &text, err, errlen))
		return -1;
	rc = adaptive_parse_conf(text, conf_path, calc_period_s, conf, err, errlen);
	free(text);
	if (rc)
		return -1;

	if (adaptive_read_file(conf->policies_file, &text, why, sizeof(why)))
		return _fail(err, errlen, "%s: PoliciesFile: %s", conf_path, why);
	rc = adaptive_parse_policies(text, conf->policies_file, pols, err, errlen);
	free(text);
	if (rc)
		return -1;

	if (!adaptive_find_policy(pols, conf->default_policy))
		return _fail(err, errlen, "%s line %d: DefaultPolicy: unknown policy '%s' (not in %s)",
			     conf_path, conf->default_line, conf->default_policy,
			     conf->policies_file);
	if (conf->fixed_policy[0] && !adaptive_find_policy(pols, conf->fixed_policy))
		return _fail(err, errlen, "%s line %d: FixedPolicy: unknown policy '%s' (not in %s)",
			     conf_path, conf->fixed_line, conf->fixed_policy,
			     conf->policies_file);
	for (i = 0; conf->force_alternate[0][0] && (i < 2); i++) {
		if (!adaptive_find_policy(pols, conf->force_alternate[i]))
			return _fail(err, errlen, "%s line %d: ForceAlternate: unknown policy '%s' (not in %s)",
				     conf_path, conf->force_line, conf->force_alternate[i],
				     conf->policies_file);
	}

	if (conf->fixed_policy[0]) {
		*active = adaptive_find_policy(pols, conf->fixed_policy);
		*mode = "FIXED";
	} else {
		*active = adaptive_find_policy(pols, conf->default_policy);
		*mode = "DEFAULT";
	}
	if (errlen)
		err[0] = '\0';
	return 0;
}
