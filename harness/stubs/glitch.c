/* Draws a screen with a status line ("Depth: 1"), then misbehaves on command:
 *   g  print garbage: invalid UTF-8, a C1 control (U+009B) and U+FFFD
 *   b  clear the screen and stay blank (still alive, still reading keys)
 *   s  redraw without the status line
 *   r  redraw normally
 *   f  write a 128 KB file to $STUB_FILE (default /tmp/nexus-glitch-<pid>.out) and delete
 *      it again (dies with SIGXFSZ under rlimit-tight)
 *   x  exit with status 3
 *   q  exit 0
 * It redraws on SIGWINCH like a curses game. With STUB_INPUT_LOG=<file> it only appends
 * every input byte to that file (for keystorm tests) and ignores the commands. */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <string.h>
#include "common.h"

static volatile sig_atomic_t winch;
static void on_winch(int s) { (void)s; winch = 1; }

enum mode { NORMAL, BLANK, NOSTATUS };

static void draw(enum mode m, int keys, int garbage) {
    if (m == BLANK) {
        printf("\033[2J\033[H");
        fflush(stdout);
        return;
    }
    printf("\033[2J\033[Hglitch stand-in \xe2\x94\x80\xe2\x94\x80 caf\xc3\xa9 \xe6\xbc\xa2\r\n");
    printf("keys: %d\r\n", keys);
    if (garbage)
        printf("bad: A\xc3\x28" "B\xff" "C\xc2\x9b" "D\xef\xbf\xbd" "E\r\n");
    if (m != NOSTATUS)
        printf("\033[24;1HDepth: 1   HP: 10/10");
    printf("\033[3;1H> ");
    fflush(stdout);
}

int main(void) {
    const char *logp = getenv("STUB_INPUT_LOG");
    int logfd = logp ? open(logp, O_WRONLY | O_CREAT | O_APPEND, 0644) : -1;
    enum mode m = NORMAL;
    int keys = 0, garbage = 0;
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_winch; /* no SA_RESTART: read() returns EINTR so we redraw */
    sigaction(SIGWINCH, &sa, NULL);
    stub_raw();
    draw(m, keys, garbage);
    for (;;) {
        unsigned char buf[4096];
        ssize_t n = read(0, buf, sizeof buf);
        if (n < 0 && errno == EINTR) {
            if (winch) { winch = 0; draw(m, keys, garbage); }
            continue;
        }
        if (n <= 0) return 0;
        if (logfd >= 0) {
            if (write(logfd, buf, n) != n) return 4;
            keys += n;
            draw(m, keys, garbage);
            continue;
        }
        for (ssize_t i = 0; i < n; i++) {
            keys++;
            switch (buf[i]) {
            case 'g': garbage = 1; break;
            case 'b': m = BLANK; break;
            case 's': m = NOSTATUS; break;
            case 'r': m = NORMAL; garbage = 0; break;
            case 'x': return 3;
            case 'q': return 0;
            case 'f': {
                static char block[8192], path[256];
                const char *p = getenv("STUB_FILE");
                if (!p) snprintf(path, sizeof path, "/tmp/nexus-glitch-%d.out", (int)getpid());
                FILE *f = fopen(p ? p : path, "w");
                if (!f) return 5;
                for (int k = 0; k < 16; k++) fwrite(block, 1, sizeof block, f);
                fclose(f);
                unlink(p ? p : path);
                break;
            }
            }
        }
        draw(m, keys, garbage);
    }
}
