/* Segfaults after STUB_CRASH_AFTER keys (default 3). */
#include "common.h"

static void __attribute__((noinline)) stub_deref(int *p) { *p = 42; }
static void __attribute__((noinline)) stub_turn(int keys, int limit) {
    if (keys >= limit) stub_deref((int *)(long)(keys - limit));
}

int main(void) {
    int limit = stub_limit("STUB_CRASH_AFTER", 3), keys = 0, c = 0;
    stub_raw();
    for (;;) {
        stub_screen("crasher", keys, limit, c);
        c = getchar();
        if (c == EOF) return 0;
        if (c == 'Q') return 0;
        stub_turn(++keys, limit);
    }
}
