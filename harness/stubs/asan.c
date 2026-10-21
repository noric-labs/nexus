/* heap-use-after-free after STUB_CRASH_AFTER keys (default 3). Build with
 * clang -g -fsanitize=address,undefined. Also triggers one UBSan report
 * (signed overflow, non-fatal) on the first key. */
#include "common.h"
#include <limits.h>

static char *__attribute__((noinline)) stub_alloc(void) { return malloc(16); }
static int __attribute__((noinline)) stub_use_after_free(int k) {
    char *p = stub_alloc();
    p[0] = (char)k;
    free(p);
    return p[k % 16];
}
static int __attribute__((noinline)) stub_overflow(int k) {
    volatile int big = INT_MAX;
    return big + k;
}

int main(void) {
    int limit = stub_limit("STUB_CRASH_AFTER", 3), keys = 0, c = 0, acc = 0;
    stub_raw();
    for (;;) {
        stub_screen("asan", keys, limit, c);
        c = getchar();
        if (c == EOF || c == 'Q') return 0;
        keys++;
        if (keys == 1 && getenv("STUB_UBSAN")) acc += stub_overflow(keys);
        if (keys >= limit) acc += stub_use_after_free(keys);
    }
    return acc;
}
