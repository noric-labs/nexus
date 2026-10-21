/* Segfaults after STUB_CRASH_AFTER keys (default 3). STUB_CRASH_SITE picks where:
 * `deref` (default) or `other` (a NULL store in another function), or `abort`. */
#include <string.h>
#include "common.h"

static void __attribute__((noinline)) stub_deref(int *p) { *p = 42; }
static void __attribute__((noinline)) stub_other(int *p) { p[1] = 7; }
static void __attribute__((noinline)) stub_abort(void) { abort(); }
static void __attribute__((noinline)) stub_turn(int keys, int limit, const char *site) {
    if (keys < limit) return;
    if (!strcmp(site, "other")) stub_other((int *)(long)(keys - limit));
    else if (!strcmp(site, "abort")) stub_abort();
    else stub_deref((int *)(long)(keys - limit));
}

int main(void) {
    int limit = stub_limit("STUB_CRASH_AFTER", 3), keys = 0, c = 0;
    const char *site = getenv("STUB_CRASH_SITE") ? getenv("STUB_CRASH_SITE") : "deref";
    stub_raw();
    for (;;) {
        stub_screen("crasher", keys, limit, c);
        c = getchar();
        if (c == EOF) return 0;
        if (c == 'Q') return 0;
        stub_turn(++keys, limit, site);
    }
}
