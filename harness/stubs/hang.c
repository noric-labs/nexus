/* Reads one key, then stops reading input and never redraws (alive but hung). */
#include "common.h"
#include <signal.h>

int main(void) {
    int c;
    stub_raw();
    stub_screen("hang", 0, 1, 0);
    c = getchar();
    if (c == EOF) return 0;
    printf("\r\nthinking...");
    fflush(stdout);
    for (;;) pause();
}
