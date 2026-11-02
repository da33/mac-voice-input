// 「語音輸入.app」的執行檔：在背景啟動 Python 主程式，不開終端機視窗。
//
// 用一個真正的 App 來啟動，macOS 的權限（麥克風、輸入監控、輔助使用）
// 才會記在「語音輸入」名下，而不是記在終端機。Python 是子程序，會沿用這些權限。

#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef PROJECT_DIR
#define PROJECT_DIR "."
#endif

extern char **environ;
static pid_t child = 0;

static void forward_signal(int sig) {
    if (child > 0) kill(child, sig);
}

static int has_main_script(const char *dir) {
    char path[PATH_MAX];
    snprintf(path, sizeof path, "%s/voice_type.py", dir);
    return access(path, F_OK) == 0;
}

int main(void) {
    // App 放在專案資料夾裡就用那個資料夾；被搬到別處（例如「應用程式」）就用打包時的路徑
    const char *project = PROJECT_DIR;
    char exe[PATH_MAX], dir[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) == 0 && realpath(exe, dir)) {
        for (int i = 0; i < 4; i++) {  // .../語音輸入.app/Contents/MacOS/voice-type → 往上四層
            char *slash = strrchr(dir, '/');
            if (slash) *slash = '\0';
        }
        if (has_main_script(dir)) project = dir;
    }

    char python[PATH_MAX], script[PATH_MAX], log_path[PATH_MAX];
    snprintf(python, sizeof python, "%s/.venv/bin/python", project);
    snprintf(script, sizeof script, "%s/voice_type.py", project);
    snprintf(log_path, sizeof log_path, "%s/voice_type.log", project);
    if (chdir(project) != 0) return 1;
    setenv("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", 1);
    setenv("PYTHONUNBUFFERED", "1", 1);

    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, log_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    posix_spawn_file_actions_adddup2(&actions, STDOUT_FILENO, STDERR_FILENO);

    char *argv[] = {python, script, NULL};
    if (posix_spawn(&child, python, &actions, NULL, argv, environ) != 0) {
        perror("posix_spawn");
        return 1;
    }
    signal(SIGTERM, forward_signal);
    signal(SIGINT, forward_signal);
    signal(SIGHUP, forward_signal);

    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
