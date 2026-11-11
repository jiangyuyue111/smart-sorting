/* 行协议解析 —— 和 Python 侧 sorting/core/protocol.py 逐条对应。
 *
 * 这个文件**不依赖任何硬件**，所以能在 PC 上用 gcc 直接编出来跑测试。
 * 真机上它被 main_stm32.c 调用，收到的字节流来自 UART IDLE 中断。
 */
#ifndef PROTO_H
#define PROTO_H

#include <stddef.h>

#define PROTO_LINE_MAX  64

/* 解析结果。负数是错误码，正数是命令种类。 */
enum {
    PROTO_E_EMPTY = -1,
    PROTO_E_LONG  = -2,
    PROTO_E_ARGS  = -3,
    PROTO_E_NUM   = -4,
    PROTO_E_CMD   = -5,
};

typedef enum {
    CMD_NONE = 0,
    CMD_GOTO,       /* GOTO <x> <y> */
    CMD_Z,          /* Z <mm>      */
    CMD_GRIP,       /* GRIP <mm>   */
    CMD_HOME,
    CMD_STOP,
    CMD_PING,
} cmd_kind_t;

typedef struct {
    cmd_kind_t kind;
    float      a;   /* GOTO: x   Z: 高度   GRIP: 张合位置 */
    float      b;   /* GOTO: y */
} cmd_t;

/* 解析一行（不含 '\n'）。成功返回 0 并把结果写进 out。
 * 失败返回 PROTO_E_*，out->kind 置 CMD_NONE。大小写不敏感。 */
int proto_parse(const char *line, cmd_t *out);

/* 组应答。返回写入长度（不含结尾 '\0'）；cap 不够返回 -1。
 * 调用方负责补 '\n'。 */
int proto_fmt_ack(char *buf, size_t cap, const char *word);
int proto_fmt_gripd(char *buf, size_t cap, float mm);
int proto_fmt_err(char *buf, size_t cap, const char *code, const char *text);

#endif /* PROTO_H */
