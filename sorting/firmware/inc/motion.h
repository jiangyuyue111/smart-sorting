/* 梯形加减速规划 —— 不依赖硬件，可在 PC 上测。
 *
 * 为什么要自己做规划而不是直接发脉冲：
 *   步进电机从静止直接开到 200 mm/s 会丢步，丢了没人知道，位置就永久错了。
 *   所以每次运动都要按"加速 → 匀速 → 减速"走，这个文件只算时间参数，
 *   真正的脉冲由定时器按算出来的周期产生。
 *
 * 两种曲线：
 *   梯形  距离够长，能加速到 vmax 再匀速一段          ← 大多数情况
 *   三角形 距离太短，还没到 vmax 就得开始减速          ← 料盒之间的小移动
 *
 * 判别条件：以 vmax 加速再减速所需的最小距离 = vmax²/accel。
 * 实际距离比它短，就一定是三角形。
 */
#ifndef MOTION_H
#define MOTION_H

typedef struct {
    float vmax_mm_s;    /* 最高速度 */
    float accel_mm_s2;  /* 加速度（减速用同一个值，简化） */
} motion_cfg_t;

typedef struct {
    float t_total_s;    /* 走完这段的总时间 */
    float t_accel_s;    /* 加速段（= 减速段）时长 */
    float v_peak_mm_s;  /* 实际达到的最高速度 */
    int   triangular;   /* 1 = 没加到 vmax，三角形曲线 */
} motion_plan_t;

/* 规划一段距离。dist_mm 取绝对值；v0 目前不起作用（每次运动都从静止开始）。 */
void motion_plan(float dist_mm, const motion_cfg_t *cfg, motion_plan_t *out);

/* 速度为 v 时，走 1mm 需要多少个脉冲周期。
 * steps_per_mm 由齿轮传动算出（见 machine.toml 的换算公式）。 */
float motion_step_period_us(float speed_mm_s, float steps_per_mm);

#endif /* MOTION_H */
