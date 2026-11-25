/*
 * ver_0.2 A2: the shared policy state. See adaptive_policy.h.
 *
 * One writer (the controller tick, or priority/adaptive's start-up and reconfigure) and any
 * number of readers. A mutex keeps each copy whole; a copy is about 100 bytes, so a read costs
 * well under a microsecond even on every node choice.
 */
#include <pthread.h>
#include <string.h>

#include "adaptive_policy.h"

static pthread_mutex_t state_mutex = PTHREAD_MUTEX_INITIALIZER;
static adaptive_policy_state_t state;	/* seq 0: nothing published yet */
static int published;			/* a policy is in force (not cleared) */

extern void adaptive_policy_publish(const adaptive_policy_t *policy, int64_t since)
{
	pthread_mutex_lock(&state_mutex);
	state.policy = *policy;
	state.policy.name[ADAPTIVE_NAME_LEN - 1] = '\0';
	state.since = since;
	published = 1;
	__atomic_store_n(&state.seq, state.seq + 1, __ATOMIC_RELEASE);
	pthread_mutex_unlock(&state_mutex);
}

extern void adaptive_policy_clear(void)
{
	pthread_mutex_lock(&state_mutex);
	memset(&state.policy, 0, sizeof(state.policy));
	state.since = 0;
	published = 0;
	__atomic_store_n(&state.seq, state.seq + 1, __ATOMIC_RELEASE);
	pthread_mutex_unlock(&state_mutex);
}

extern int adaptive_policy_get(adaptive_policy_state_t *out)
{
	int rc;

	pthread_mutex_lock(&state_mutex);
	*out = state;
	rc = published ? 0 : -1;
	pthread_mutex_unlock(&state_mutex);
	return rc;
}

extern uint64_t adaptive_policy_seq(void)
{
	return __atomic_load_n(&state.seq, __ATOMIC_ACQUIRE);
}
