#include "proto.h"

#include <string.h>

/* ------------------------------------------------------------------ 小工具 */

static char upper(char c)
{
    return (c >= 'a' && c <= 'z') ? (char)(c - 'a' + 'A') : c;
}

static int eq_ci(const char *a, const char *b)
{
    while (*a && *b) {
        if (upper(*a) != upper(*b)) {
            return 0;
        }
        a++;
        b++;
    }
    return *a == '\0' && *b == '\0';
}

/* 不用 strtof：newlib 的 strtof 会拖进一大堆浮点/本地化代码，
 * 而且行为受 locale 影响。协议只有十进制小数，自己解析更可控。 */
static int parse_num(const char *s, float *out)
{
    int   neg = 0;
    int   seen_digit = 0;
    int   seen_dot = 0;
    float v = 0.0f;
    float scale = 1.0f;

    if (*s == '-') {
        neg = 1;
        s++;
    } else if (*s == '+') {
        s++;
    }

    for (; *s; s++) {
        if (*s >= '0' && *s <= '9') {
            /* 超过 7 位有效数字就没有意义了（协议只给一位小数） */
            if (v < 1.0e7f) {
                v = v * 10.0f + (float)(*s - '0');
                if (seen_dot) {
                    scale *= 10.0f;
                }
            }
            seen_digit = 1;
        } else if (*s == '.' && !seen_dot) {
            seen_dot = 1;
        } else {
            return -1;
        }
    }

    if (!seen_digit) {
        return -1;
    }
    *out = neg ? -(v / scale) : (v / scale);
    return 0;
}

/* 把一个非负浮点写成 "%.1f"，不用 printf 以避开 newlib 的浮点格式化开销。
 * 四舍五入到 0.1mm，和 Python 侧 f"{x:.1f}" 一致。 */
static int fmt_one_decimal(char *buf, size_t cap, float mm)
{
    int   neg = 0;
    long  tenths;
    long  ip;
    long  fp;
    char  tmp[16];
    int   n = 0;
    int   i;

    if (mm < 0.0f) {
        neg = 1;
        mm = -mm;
    }
    /* 加 0.5 个最小单位实现四舍五入 */
    tenths = (long)(mm * 10.0f + 0.5f);
    ip = tenths / 10;
    fp = tenths % 10;

    if (tenths == 0) {
        neg = 0;
    }

    tmp[n++] = (char)('0' + (fp % 10));
    tmp[n++] = '.';

    if (ip == 0) {
        tmp[n++] = '0';
    }
    while (ip > 0) {
        if ((size_t)n >= sizeof(tmp) - 1) {
            return -1;
        }
        tmp[n++] = (char)('0' + (ip % 10));
        ip /= 10;
    }
    if (neg) {
        if ((size_t)n >= sizeof(tmp) - 1) {
            return -1;
        }
        tmp[n++] = '-';
    }

    if ((size_t)n + 1 > cap) {
        return -1;
    }
    for (i = 0; i < n; i++) {
        buf[i] = tmp[n - 1 - i];
    }
    buf[n] = '\0';
    return n;
}

static int fmt_str(char *buf, size_t cap, const char *s)
{
    size_t n = strlen(s);
    if (n + 1 > cap) {
        return -1;
    }
    memcpy(buf, s, n + 1);
    return (int)n;
}

/* ------------------------------------------------------------------ 解析 */

int proto_parse(const char *line, cmd_t *out)
{
    char  buf[PROTO_LINE_MAX];
    char *tok[4];
    int   ntok = 0;
    size_t n;
    char *s;

    out->kind = CMD_NONE;
    out->a = 0.0f;
    out->b = 0.0f;

    if (line == NULL) {
        return PROTO_E_EMPTY;
    }
    n = strlen(line);
    if (n == 0) {
        return PROTO_E_EMPTY;
    }
    if (n >= sizeof(buf)) {
        return PROTO_E_LONG;
    }
    memcpy(buf, line, n + 1);

    s = buf;
    while (*s) {
        while (*s == ' ' || *s == '\t' || *s == '\r') {
            s++;
        }
        if (*s == '\0') {
            break;
        }
        if (ntok == 4) {
            return PROTO_E_ARGS;
        }
        tok[ntok++] = s;
        while (*s && *s != ' ' && *s != '\t' && *s != '\r') {
            s++;
        }
        if (*s) {
            *s++ = '\0';
        }
    }

    if (ntok == 0) {
        return PROTO_E_EMPTY;
    }

    if (eq_ci(tok[0], "PING")) {
        if (ntok != 1) {
            return PROTO_E_ARGS;
        }
        out->kind = CMD_PING;
        return 0;
    }
    if (eq_ci(tok[0], "HOME")) {
        if (ntok != 1) {
            return PROTO_E_ARGS;
        }
        out->kind = CMD_HOME;
        return 0;
    }
    if (eq_ci(tok[0], "STOP")) {
        if (ntok != 1) {
            return PROTO_E_ARGS;
        }
        out->kind = CMD_STOP;
        return 0;
    }
    if (eq_ci(tok[0], "GOTO")) {
        if (ntok != 3) {
            return PROTO_E_ARGS;
        }
        if (parse_num(tok[1], &out->a) != 0 || parse_num(tok[2], &out->b) != 0) {
            return PROTO_E_NUM;
        }
        out->kind = CMD_GOTO;
        return 0;
    }
    if (eq_ci(tok[0], "Z")) {
        if (ntok != 2) {
            return PROTO_E_ARGS;
        }
        if (parse_num(tok[1], &out->a) != 0) {
            return PROTO_E_NUM;
        }
        out->kind = CMD_Z;
        return 0;
    }
    if (eq_ci(tok[0], "GRIP")) {
        if (ntok != 2) {
            return PROTO_E_ARGS;
        }
        if (parse_num(tok[1], &out->a) != 0) {
            return PROTO_E_NUM;
        }
        out->kind = CMD_GRIP;
        return 0;
    }

    return PROTO_E_CMD;
}

/* ------------------------------------------------------------------ 组包 */

int proto_fmt_ack(char *buf, size_t cap, const char *word)
{
    return fmt_str(buf, cap, word);
}

int proto_fmt_gripd(char *buf, size_t cap, float mm)
{
    int n = fmt_str(buf, cap, "GRIPD ");
    int m;
    if (n < 0) {
        return -1;
    }
    m = fmt_one_decimal(buf + n, cap - (size_t)n, mm);
    if (m < 0) {
        return -1;
    }
    return n + m;
}

int proto_fmt_err(char *buf, size_t cap, const char *code, const char *text)
{
    int n = fmt_str(buf, cap, "ERR ");
    int m;
    if (n < 0) {
        return -1;
    }
    m = fmt_str(buf + n, cap - (size_t)n, code);
    if (m < 0) {
        return -1;
    }
    n += m;
    if (text != NULL && *text != '\0') {
        m = fmt_str(buf + n, cap - (size_t)n, " ");
        if (m < 0) {
            return -1;
        }
        n += m;
        m = fmt_str(buf + n, cap - (size_t)n, text);
        if (m < 0) {
            return -1;
        }
        n += m;
    }
    return n;
}
