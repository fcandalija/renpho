/*
 * RENPHO ingest launcher.
 *
 * launchd runs this compiled binary as the app bundle's executable, giving TCC a
 * stable identity to attach Full Disk Access to. It then *spawns* python as a
 * child (posix_spawn, not exec) and waits, so this binary remains the process's
 * "responsible process" and the python child inherits the app's FDA grant — the
 * same way commands launched from Terminal inherit Terminal's file access.
 *
 * Built as a universal binary so one app runs on both Apple Silicon and Intel
 * (see build.sh). Nothing here is arch-specific, but two things used to be
 * machine-specific and are now resolved at runtime, because the same app runs on
 * an Apple Silicon Mac and a 2014 Intel Mac mini:
 *
 *   - python3's location: Homebrew is /opt/homebrew on Apple Silicon but
 *     /usr/local on Intel, so the candidates are tried in order.
 *   - the repo path: derived from this binary's own location rather than
 *     hardcoded, so the checkout can live anywhere on either machine.
 */
#include <spawn.h>
#include <sys/wait.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <stdio.h>
#include <stdint.h>
#include <limits.h>
#include <mach-o/dyld.h>

extern char **environ;

/* Used only if the repo path can't be derived from the executable location. */
#define FALLBACK_REPO "/Users/fcandalija/repos/renpho"

/* Both Homebrew prefixes, so `tesseract` (needed by the OCR step) resolves
   whichever machine this runs on. launchd gives us a minimal PATH otherwise. */
static const char PATH_VALUE[] =
    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";

static const char *const PYTHONS[] = {
    "/opt/homebrew/bin/python3",   /* Homebrew on Apple Silicon */
    "/usr/local/bin/python3",      /* Homebrew on Intel */
    "/usr/bin/python3",            /* system python3 (needs Command Line Tools) */
    NULL,
};

/* Strip n trailing path components in place. Returns 0 if it ran out of path. */
static int strip_components(char *p, int n) {
    for (int i = 0; i < n; i++) {
        char *slash = strrchr(p, '/');
        if (slash == NULL || slash == p) return 0;
        *slash = '\0';
    }
    return 1;
}

int main(void) {
    setenv("PATH", PATH_VALUE, 1);

    /* The binary sits at <repo>/RENPHO Ingest.app/Contents/MacOS/renpho-ingest,
       so the repo is 4 components up. */
    char script[PATH_MAX];
    char exe[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) == 0 && strip_components(exe, 4))
        snprintf(script, sizeof script, "%s/process_pdfs.py", exe);
    else
        snprintf(script, sizeof script, "%s/process_pdfs.py", FALLBACK_REPO);

    /* $RENPHO_PYTHON wins if it's usable; otherwise take the first candidate
       that exists and is executable. */
    const char *py = getenv("RENPHO_PYTHON");
    if (py == NULL || access(py, X_OK) != 0) {
        py = NULL;
        for (int i = 0; PYTHONS[i] != NULL; i++) {
            if (access(PYTHONS[i], X_OK) == 0) { py = PYTHONS[i]; break; }
        }
    }
    if (py == NULL) {
        fprintf(stderr, "renpho-ingest: no usable python3 found (tried "
                        "/opt/homebrew/bin, /usr/local/bin, /usr/bin); "
                        "set $RENPHO_PYTHON\n");
        return 127;
    }

    char *const argv[] = { (char *)py, script, "ingest", NULL };
    pid_t pid;
    int rc = posix_spawn(&pid, argv[0], NULL, NULL, argv, environ);
    if (rc != 0) {
        fprintf(stderr, "renpho-ingest: could not spawn %s: %s\n",
                argv[0], strerror(rc));
        return rc;
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) return 1;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
