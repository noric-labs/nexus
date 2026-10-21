/* Shared bits for the stand-in "games": raw keyboard input and a tiny screen. */
#include <stdio.h>
#include <stdlib.h>
#include <termios.h>
#include <unistd.h>

static struct termios stub_saved;

static void stub_restore(void) { tcsetattr(0, TCSANOW, &stub_saved); }

static void stub_raw(void) {
    struct termios t;
    if (tcgetattr(0, &t) != 0) return;
    stub_saved = t;
    t.c_lflag &= ~(ICANON | ECHO);
    t.c_cc[VMIN] = 1;
    t.c_cc[VTIME] = 0;
    tcsetattr(0, TCSANOW, &t);
    atexit(stub_restore);
}

static void stub_screen(const char *name, int keys, int limit, int last) {
    printf("\033[2J\033[H%s stand-in\r\n", name);
    printf("keys: %d / %d   last: %d\r\n", keys, limit, last);
    printf("> ");
    fflush(stdout);
}

static int stub_limit(const char *env, int dflt) {
    const char *v = getenv(env);
    return v && *v ? atoi(v) : dflt;
}
