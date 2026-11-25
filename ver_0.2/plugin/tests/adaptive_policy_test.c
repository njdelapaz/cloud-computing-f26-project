/*
 * Test of the shared policy state (plugin/adaptive_policy/), set up the way slurmctld uses it.
 * Built three ways by plugin/tests/test_adaptive_policy.py:
 *   -DHOST    the "slurmctld": links adaptive_policy.c and exports its symbols (-rdynamic, as
 *             slurmctld's -export-dynamic does), then loads the two plugins with
 *             dlopen(RTLD_LAZY), as Slurm's plugin loader does (local symbols, no RTLD_GLOBAL)
 *   -DWRITER  a "priority/adaptive": publishes, without its own copy of the module
 *   -DREADER  a "select/cons_tres_adaptive": reads, without its own copy of the module
 * Usage: adaptive_policy_test <writer.so> <reader.so>. Prints one "ok ..." line per check, or
 * "FAIL ..." and exits 1.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "adaptive_policy.h"

#if defined(WRITER)
void plugin_publish(int k, int64_t since)
{
	adaptive_policy_t p;

	memset(&p, 0, sizeof(p));
	snprintf(p.name, sizeof(p.name), "P%d", k);
	p.fairshare = (uint32_t) k;
	p.age = 100000 - (uint32_t) k;
	p.spread = k / 100000.0;
	p.fit = 1.0 - p.spread;
	adaptive_policy_publish(&p, since);
}

void plugin_clear(void)
{
	adaptive_policy_clear();
}

#elif defined(READER)
/* 0 when nothing is published; otherwise k, checking that the copy is whole */
int plugin_read(uint64_t *seq, int64_t *since)
{
	adaptive_policy_state_t s;
	char want[ADAPTIVE_NAME_LEN];
	int k;

	if (adaptive_policy_get(&s)) {
		*seq = s.seq;
		return 0;
	}
	*seq = s.seq;
	*since = s.since;
	k = (int) s.policy.fairshare;
	snprintf(want, sizeof(want), "P%d", k);
	if (strcmp(s.policy.name, want) || (s.policy.age != 100000 - (uint32_t) k) ||
	    (s.policy.spread != k / 100000.0) || (s.policy.fit != 1.0 - s.policy.spread))
		return -1;	/* torn copy */
	return k;
}

#elif defined(HOST)
#include <dlfcn.h>
#include <pthread.h>

typedef void (*publish_fn)(int, int64_t);
typedef void (*clear_fn)(void);
typedef int (*read_fn)(uint64_t *, int64_t *);

static read_fn rd;
static volatile int stop;
static int torn, reads;

static void *_reader_thread(void *arg)
{
	uint64_t seq;
	int64_t since;

	(void) arg;
	while (!stop) {
		if (rd(&seq, &since) < 0)
			__atomic_add_fetch(&torn, 1, __ATOMIC_RELAXED);
		__atomic_add_fetch(&reads, 1, __ATOMIC_RELAXED);
	}
	return NULL;
}

#define CHECK(cond, ...) do {						\
	if (!(cond)) { printf("FAIL "); printf(__VA_ARGS__); printf("\n"); return 1; } \
	printf("ok "); printf(__VA_ARGS__); printf("\n");		\
} while (0)

int main(int argc, char **argv)
{
	void *w, *r;
	publish_fn pub;
	clear_fn clr;
	uint64_t seq = 99, seq2;
	int64_t since = 0;
	adaptive_policy_state_t mine;
	pthread_t th[4];
	int i, k;

	if (argc != 3) {
		fprintf(stderr, "usage: %s <writer.so> <reader.so>\n", argv[0]);
		return 2;
	}
	/* the reader is loaded first, the way select plugins load before the priority plugin */
	if (!(r = dlopen(argv[2], RTLD_LAZY)) || !(w = dlopen(argv[1], RTLD_LAZY))) {
		printf("FAIL dlopen: %s\n", dlerror());
		return 1;
	}
	pub = (publish_fn) dlsym(w, "plugin_publish");
	clr = (clear_fn) dlsym(w, "plugin_clear");
	rd = (read_fn) dlsym(r, "plugin_read");
	CHECK(pub && clr && rd, "both plugins loaded with RTLD_LAZY and their entry points found");

	CHECK(rd(&seq, &since) == 0 && seq == 0, "before any publish the reader sees nothing (seq 0)");
	CHECK(adaptive_policy_get(&mine) == -1, "and so does the host");

	pub(54000, 1704067260);
	k = rd(&seq, &since);
	CHECK(k == 54000 && seq == 1 && since == 1704067260,
	      "the reader sees what the writer published (k=%d seq=%llu since=%lld)",
	      k, (unsigned long long) seq, (long long) since);
	CHECK(adaptive_policy_get(&mine) == 0 && !strcmp(mine.policy.name, "P54000"),
	      "the host sees the same copy (%s), so there is one state in the process",
	      mine.policy.name);
	CHECK(adaptive_policy_seq() == 1, "seq counts publishes");

	pub(90000, 1704067320);
	k = rd(&seq2, &since);
	CHECK(k == 90000 && seq2 == seq + 1, "a second publish replaces the first and moves seq");

	clr();
	k = rd(&seq, &since);
	CHECK(k == 0 && seq == seq2 + 1, "clear: the reader falls back to nothing, and seq still moves");

	/* readers on four threads while the writer publishes: every copy must be whole */
	for (i = 0; i < 4; i++)
		pthread_create(&th[i], NULL, _reader_thread, NULL);
	for (k = 1; k <= 200000; k++)
		pub(k % 100001, k);
	stop = 1;
	for (i = 0; i < 4; i++)
		pthread_join(th[i], NULL);
	CHECK(torn == 0 && reads > 0, "%d reads on 4 threads during 200000 publishes, %d torn copies",
	      reads, torn);
	return 0;
}
#else
#error "build with -DHOST, -DWRITER or -DREADER"
#endif
