#include "motion.h"

#include <math.h>
#include <stddef.h>

void motion_plan(float dist_mm, const motion_cfg_t *cfg, motion_plan_t *out)
{
    float d;
    float vmax;
    float accel;
    float v_tri;

    out->t_total_s = 0.0f;
    out->t_accel_s = 0.0f;
    out->v_peak_mm_s = 0.0f;
    out->triangular = 0;

    if (cfg == NULL || out == NULL) {
        return;
    }
    d = (dist_mm < 0.0f) ? -dist_mm : dist_mm;
    vmax = cfg->vmax_mm_s;
    accel = cfg->accel_mm_s2;

    if (d <= 0.0f || vmax <= 0.0f || accel <= 0.0f) {
        return;
    }

    /* 加速到 vmax 再减速回来所需的距离 = vmax²/accel */
    v_tri = sqrtf(accel * d);

    if (v_tri >= vmax) {
        /* 梯形：加速 vmax/a，匀速，减速 vmax/a */
        out->v_peak_mm_s = vmax;
        out->t_accel_s = vmax / accel;
        out->t_total_s = d / vmax + vmax / accel;
        out->triangular = 0;
    } else {
        /* 三角形：峰值速度达不到 vmax，总时间 = 2v/a */
        out->v_peak_mm_s = v_tri;
        out->t_accel_s = v_tri / accel;
        out->t_total_s = 2.0f * out->t_accel_s;
        out->triangular = 1;
    }
}

float motion_step_period_us(float speed_mm_s, float steps_per_mm)
{
    float spm;

    if (speed_mm_s <= 0.0f || steps_per_mm <= 0.0f) {
        return 0.0f;
    }
    spm = speed_mm_s * steps_per_mm;   /* 每秒钟多少个脉冲 */
    if (spm <= 1.0f) {
        return 1.0e6f;
    }
    return 1.0e6f / spm;
}
