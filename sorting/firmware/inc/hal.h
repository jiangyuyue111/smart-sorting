/* 硬件抽象层 —— main_stm32.c 实现它，machine.c 只调它。
 *
 * 这样状态机不依赖 HAL 库、不依赖寄存器，可以在 PC 上用一个假 HAL
 * 把"收到什么指令、回什么包"整套逻辑测完。真机上换的只是这张函数表。
 *
 * 四个轴的编号固定：
 */
#ifndef HAL_H
#define HAL_H

enum { AX_X = 0, AX_Y = 1, AX_Z = 2, AX_GRIP = 3, AX_COUNT = 4 };

typedef struct {
    /* 发一行（不带 '\n'，本层负责补），必须是非阻塞的（DMA 或环形缓冲） */
    void  (*reply)(const char *line);

    /* 启动一次运动。steps 有符号，负 = 反向。返回 0 = 已启动，负 = 拒绝。
     * 平台侧负责按 motion_plan 的结果驱动定时器，并在走完后自己停。 */
    int   (*begin_move)(int axis, long steps, float v_peak_mm_s, float accel_mm_s2);

    /* 该轴是否还在动（0 = 停稳） */
    int   (*is_moving)(int axis);

    /* 当前绝对位置（mm），由脉冲计数换算 */
    float (*position_mm)(int axis);

    /* 各轴最大行程（mm），用于超程检查 */
    float (*max_mm)(int axis);

    /* 启动回零。返回 0 = 已启动。 */
    int   (*home)(void);

    /* 回零过程中是否撞上了"找不到限位"的失败（1 = 失败） */
    int   (*home_failed)(void);

    /* 立即停止所有轴（不影响上电状态） */
    void  (*stop_all)(void);

    /* 夹爪实际停止位置（mm）。堵转即停，所以这个值说明了夹到多厚的件。 */
    float (*grip_actual_mm)(void);
} hal_t;

#endif /* HAL_H */
