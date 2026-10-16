/* Restrict the on-device executor before ART creates any threads.
 * This is a process credential boundary, not a filesystem/network sandbox. */
#define _GNU_SOURCE
#include <errno.h>
#include <grp.h>
#include <linux/capability.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

static void fail(const char *operation) {
    perror(operation);
    exit(126);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: mu-android-shell PROGRAM [ARG ...]\n");
        return 126;
    }
    if (getuid() != geteuid() || (geteuid() != 0 && geteuid() != 2000)) {
        fprintf(stderr, "mu-android-shell requires root or shell\n");
        return 126;
    }
    if (prctl(PR_SET_NO_NEW_PRIVS, 1L, 0L, 0L, 0L)) fail("no_new_privs");
    if (geteuid() == 0) {
        /* Match ordinary ADB shell groups, never inherit arbitrary root groups. */
        gid_t groups[] = {1004, 1007, 1011, 1015, 1028, 1078, 1079,
                          3001, 3002, 3003, 3006, 3009, 3011, 3012};
        if (setgroups(sizeof(groups) / sizeof(groups[0]), groups)) fail("setgroups");
        for (int cap = 0;; cap++) {
            int present = prctl(PR_CAPBSET_READ, cap, 0L, 0L, 0L);
            if (present < 0) {
                if (errno == EINVAL) break;
                fail("read capability bounding set");
            }
            if (prctl(PR_CAPBSET_DROP, cap, 0L, 0L, 0L)) fail("drop capability");
        }
        if (prctl(PR_SET_KEEPCAPS, 0L, 0L, 0L, 0L)) fail("clear keepcaps");
        if (setresgid(2000, 2000, 2000)) fail("setresgid");
        if (setresuid(2000, 2000, 2000)) fail("setresuid");
    }
    struct __user_cap_header_struct header = {_LINUX_CAPABILITY_VERSION_3, 0};
    struct __user_cap_data_struct caps[2] = {{0}, {0}};
    if (syscall(SYS_capset, &header, caps)) fail("clear capabilities");
    if (syscall(SYS_capget, &header, caps)) fail("verify capabilities");
    uid_t real_uid, effective_uid, saved_uid;
    gid_t real_gid, effective_gid, saved_gid;
    if (getresuid(&real_uid, &effective_uid, &saved_uid)) fail("getresuid");
    if (getresgid(&real_gid, &effective_gid, &saved_gid)) fail("getresgid");
    if (real_uid != 2000 || effective_uid != 2000 || saved_uid != 2000 ||
        real_gid != 2000 || effective_gid != 2000 || saved_gid != 2000 ||
        caps[0].effective || caps[0].permitted || caps[0].inheritable ||
        caps[1].effective || caps[1].permitted || caps[1].inheritable ||
        prctl(PR_GET_NO_NEW_PRIVS, 0L, 0L, 0L, 0L) != 1) {
        fprintf(stderr, "mu-android-shell: unsafe execution identity\n");
        return 126;
    }
    if (setenv("MAI_UI_ANDROID_REQUIRE_SHELL", "1", 1)) fail("setenv");
    execv(argv[1], &argv[1]);
    fail("execv");
}
