#include "machine.h"

#include <string.h>

#include "motion.h"
#include "proto.h"

static void say(machine_t *m, const char *line)
{
    if (m->hal != NULL && m->hal->reply != NULL) {
        m->hal->reply(line);
    }
}

static void say_ack(machine_t *m, const char *word)
{
    char buf[PROTO_LINE_MAX];
    if (proto_fmt_ack(buf, sizeof buf, word) >= 0) {
        say(m, buf);
    }
}

static void say_err(machine_t *m, const char *code, const char *text)
{
    char buf[PROTO_LINE_MAX];
    if (proto_fmt_err(buf, sizeof buf, code, text) >= 0) {
        say(m, buf);
    }
    if (strcmp(code, "E01") == 0 || strcmp(code, "E02") == 0) {
        /* 超程和找不到限位意味着位置已不可信，必须停下来等人处理。
         * （E03/E04 不停机：上位机拼错一行、或某件被卡住夹爪，
         *   都不该让整台机器趴下——这是 docs/软件方案.md §9 的优先级。） */
        if (m->state != M_FAULT) {
            m->state = M_FAULT;
        }
    }
}

static int all_idle(machine_t *m)
{
    int a;
    for (a = 0; a < AX_COUNT; a++) {
        if (m->hal->is_moving(a)) {
            return 0;
        }
    }
    return 1;
}

static int out_of_range(machine_t *m, int axis, float target)
{
    float lim;
    if (m->hal->max_mm == NULL) {
        return 0;
    }
    lim = m->hal->max_mm(axis);
    if (target < 0.0f || target > lim) {
        return 1;
    }
    return 0;
}

static long mm_to_steps(const machine_t *m, int axis, float delta_mm)
{
    float spm = m->cfg->steps_per_mm[axis];
    float n = delta_mm * spm;
    /* 四舍五入到整步，别让累积误差反噬位置 */
    return (long)((n >= 0.0f) ? (n + 0.5f) : (n - 0.5f));
}

/* 启一根轴走到 target。返回 0 = 已启动。 */
static int move_axis(machine_t *m, int axis, float target)
{
    float cur = m->hal->position_mm(axis);
    float delta = target - cur;
    motion_cfg_t mc;
    motion_plan_t mp;
    long steps;

    if (delta > -1.0e-4f && delta < 1.0e-4f) {
        return 0;   /* 已经在那儿了，不用动 */
    }

    mc.vmax_mm_s = m->cfg->vmax_mm_s[axis];
    mc.accel_mm_s2 = m->cfg->accel_mm_s2;
    motion_plan(delta, &mc, &mp);

    steps = mm_to_steps(m, axis, delta);
    if (steps == 0) {
        return 0;
    }
    return m->hal->begin_move(axis, steps, mp.v_peak_mm_s, m->cfg->accel_mm_s2);
}

void machine_init(machine_t *m, const hal_t *hal, const machine_cfg_t *cfg,
                  uint32_t now_ms)
{
    memset(m, 0, sizeof *m);
    m->hal = hal;
    m->cfg = cfg;
    m->state = M_IDLE;
    m->t_state_ms = now_ms;
}

void machine_on_line(machine_t *m, const char *line, uint32_t now_ms)
{
    cmd_t cmd;
    int rc;

    if (m->state == M_FAULT) {
        /* 协议里没有专门的"已故障"码，任何 ERR 上位机都当致命处理
         * （见 Python 侧 link.Session._wait_for -> McuFault），所以这里
         * 复用一个码即可。要恢复只能断电重启，没有远程复位——故意的：
         * 位置都不可信了，远程复位等于蒙着眼睛继续跑。 */
        say_err(m, "E01", "machine fault");
        return;
    }

    rc = proto_parse(line, &cmd);
    if (rc != 0) {
        /* 未知指令 / 参数不对 —— 回 E03，但**不进 FAULT**：
         * 上位机拼错一行不该让整台机器停机。 */
        say_err(m, "E03", (rc == PROTO_E_NUM) ? "bad number"
                                              : (rc == PROTO_E_ARGS) ? "bad args"
                                                                     : "unknown");
        return;
    }

    switch (cmd.kind) {
    case CMD_PING:
        say_ack(m, "OK");
        say_ack(m, "PONG");
        return;

    case CMD_STOP:
        m->hal->stop_all();
        m->state = M_IDLE;
        m->t_state_ms = now_ms;
        say_ack(m, "OK");
        return;

    case CMD_HOME:
        if (m->hal->home() != 0) {
            say_err(m, "E02", "home refused");
            return;
        }
        m->state = M_HOMING;
        m->t_state_ms = now_ms;
        say_ack(m, "OK");
        return;

    case CMD_GOTO:
        if (out_of_range(m, AX_X, cmd.a) || out_of_range(m, AX_Y, cmd.b)) {
            say_err(m, "E01", "goto out of range");
            return;
        }
        /* XY 联动：两根轴同时启动，各自按自己的梯形曲线走。
         * 某根动不了就两根都停，别留下半截运动状态。 */
        if (move_axis(m, AX_X, cmd.a) != 0 || move_axis(m, AX_Y, cmd.b) != 0) {
            m->hal->stop_all();
            say_err(m, "E01", "move refused");
            return;
        }
        m->state = M_MOVING;
        m->t_state_ms = now_ms;
        say_ack(m, "OK");
        return;

    case CMD_Z:
        if (out_of_range(m, AX_Z, cmd.a)) {
            say_err(m, "E01", "z out of range");
            return;
        }
        if (move_axis(m, AX_Z, cmd.a) != 0) {
            say_err(m, "E01", "move refused");
            return;
        }
        m->state = M_MOVING;
        m->t_state_ms = now_ms;
        say_ack(m, "OK");
        return;

    case CMD_GRIP:
        if (out_of_range(m, AX_GRIP, cmd.a)) {
            say_err(m, "E01", "grip out of range");
            return;
        }
        if (move_axis(m, AX_GRIP, cmd.a) != 0) {
            say_err(m, "E01", "move refused");
            return;
        }
        m->state = M_GRIP;
        m->t_state_ms = now_ms;
        say_ack(m, "OK");
        return;

    default:
        say_err(m, "E03", "unknown");
        return;
    }
}

void machine_tick(machine_t *m, uint32_t now_ms)
{
    uint32_t dt = now_ms - m->t_state_ms;
    char buf[PROTO_LINE_MAX];

    switch (m->state) {
    case M_IDLE:
    case M_FAULT:
        return;

    case M_MOVING:
        if (all_idle(m)) {
            m->state = M_IDLE;
            m->t_state_ms = now_ms;
            say_ack(m, "DONE");
        } else if (dt > m->cfg->move_timeout_ms) {
            m->hal->stop_all();
            m->state = M_IDLE;
            m->t_state_ms = now_ms;
            say_err(m, "E01", "move timeout");
        }
        return;

    case M_HOMING:
        if (m->hal->home_failed != NULL && m->hal->home_failed()) {
            m->state = M_FAULT;
            say_err(m, "E02", "limit not found");
        } else if (all_idle(m)) {
            m->state = M_IDLE;
            m->t_state_ms = now_ms;
            say_ack(m, "DONE");
        } else if (dt > m->cfg->home_timeout_ms) {
            m->hal->stop_all();
            m->state = M_FAULT;
            say_err(m, "E02", "home timeout");
        }
        return;

    case M_GRIP:
        if (all_idle(m)) {
            m->state = M_IDLE;
            m->t_state_ms = now_ms;
            say_ack(m, "DONE");
            /* DONE 之后立刻报实际停止位置：堵转即停，这个值决定了有没有夹到件。
             * 上位机 Session.grip 就是等这一包。 */
            if (proto_fmt_gripd(buf, sizeof buf, m->hal->grip_actual_mm()) >= 0) {
                say(m, buf);
            }
        } else if (dt > m->cfg->grip_timeout_ms) {
            m->hal->stop_all();
            m->state = M_IDLE;
            m->t_state_ms = now_ms;
            say_err(m, "E04", "grip timeout");
        }
        return;
    }
}
