/* 指令状态机 —— 只做"收到一行 -> 决定动作 -> 回什么包"，不碰寄存器。
 *
 * 时序约定（和 Python 侧 Session 严格对齐）：
 *     收到指令           -> 立刻回 OK
 *     运动走完           -> 回 DONE
 *     夹爪走完           -> 先 DONE，再 GRIPD <实际停止位置>
 *     任何超时/超程      -> 回 ERR <code> <text>
 *
 * 这里的"超时"和上位机的超时是两道独立的闸：上位机那 15 秒是防 MCU 死掉，
 * 这里几百毫秒到几秒是防某一根轴被卡住。两边都保留。
 */
#ifndef MACHINE_H
#define MACHINE_H

#include <stdint.h>

#include "hal.h"

typedef struct {
    float    steps_per_mm[AX_COUNT];
    float    vmax_mm_s[AX_COUNT];
    float    accel_mm_s2;
    uint32_t move_timeout_ms;
    uint32_t grip_timeout_ms;
    uint32_t home_timeout_ms;
} machine_cfg_t;

typedef enum {
    M_IDLE = 0,
    M_MOVING,
    M_HOMING,
    M_GRIP,
    M_FAULT,
} mstate_t;

typedef struct {
    const hal_t        *hal;
    const machine_cfg_t *cfg;
    mstate_t            state;
    uint32_t            t_state_ms;
} machine_t;

void machine_init(machine_t *m, const hal_t *hal, const machine_cfg_t *cfg,
                  uint32_t now_ms);

/* 喂一行（不含 '\n'）。语法错误也在这里回 ERR，不返回给调用方。 */
void machine_on_line(machine_t *m, const char *line, uint32_t now_ms);

/* 周期性调用（建议 1ms 定时器或主循环）。负责判完成、判超时。 */
void machine_tick(machine_t *m, uint32_t now_ms);

#endif /* MACHINE_H */
