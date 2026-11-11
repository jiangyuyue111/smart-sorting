/* 固件纯逻辑的主机测试 —— 不需要 STM32，gcc 编出来直接跑。
 *
 * 覆盖三块：
 *   proto.c    行解析 / 组包，必须和 Python 侧逐字对齐
 *   motion.c   梯形/三角形速度曲线
 *   machine.c  指令状态机（用一个假 HAL 驱动）
 *
 * 真机上跑不起来的部分（UART 中断、定时器 PWM、TMC2209、限位）不在测试范围，
 * 那些只能上板验。
 */

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "machine.h"
#include "motion.h"
#include "proto.h"

static int g_fail = 0;
static int g_pass = 0;

#define CHECK(cond)                                                          \
    do {                                                                     \
        if (cond) {                                                          \
            g_pass++;                                                        \
        } else {                                                             \
            g_fail++;                                                        \
            printf("  FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);         \
        }                                                                    \
    } while (0)

#define CHECK_NEAR(a, b, tol) CHECK(fabsf((a) - (b)) <= (tol))

/* ------------------------------------------------------------------ 假 HAL */

#define MAX_REPLY 32

static char   g_reply[MAX_REPLY][PROTO_LINE_MAX];
static int    g_nreply;
static float  g_pos[AX_COUNT];
static int    g_moving[AX_COUNT];
static float  g_max[AX_COUNT] = {273.0f, 200.0f, 80.0f, 40.0f};
static float  g_grip_actual;
static int    g_home_failed;
static long   g_last_steps[AX_COUNT];

static void reset_fake(void)
{
    memset(g_reply, 0, sizeof g_reply);
    g_nreply = 0;
    memset(g_pos, 0, sizeof g_pos);
    memset(g_moving, 0, sizeof g_moving);
    memset(g_last_steps, 0, sizeof g_last_steps);
    g_grip_actual = 0.0f;
    g_home_failed = 0;
}

static void fake_reply(const char *line)
{
    if (g_nreply < MAX_REPLY) {
        strncpy(g_reply[g_nreply], line, PROTO_LINE_MAX - 1);
        g_nreply++;
    }
}

static int fake_begin_move(int axis, long steps, float vpeak, float accel)
{
    (void)vpeak;
    if (accel <= 0.0f) {
        return -1;
    }
    g_last_steps[axis] = steps;
    g_moving[axis] = 1;
    return 0;
}

static int   fake_is_moving(int axis) { return g_moving[axis]; }
static float fake_position(int axis)  { return g_pos[axis]; }
static float fake_max(int axis)       { return g_max[axis]; }
static int   fake_home(void)          { g_moving[AX_X] = 1; return 0; }
static int   fake_home_failed(void)   { return g_home_failed; }
static void  fake_stop_all(void)      { memset(g_moving, 0, sizeof g_moving); }
static float fake_grip_actual(void)   { return g_grip_actual; }

/* 假装所有轴都走完了，并把位置更新到目标 */
static void finish_motion(machine_t *m, float x, float y, float z, float g)
{
    g_pos[AX_X] = x;
    g_pos[AX_Y] = y;
    g_pos[AX_Z] = z;
    g_pos[AX_GRIP] = g;
    memset(g_moving, 0, sizeof g_moving);
    machine_tick(m, 1000u);
}

static const hal_t FAKE_HAL = {
    fake_reply, fake_begin_move, fake_is_moving, fake_position,
    fake_max, fake_home, fake_home_failed, fake_stop_all, fake_grip_actual,
};

static const machine_cfg_t CFG = {
    .steps_per_mm = {50.93f, 50.93f, 127.3f, 63.66f},
    .vmax_mm_s = {200.0f, 200.0f, 100.0f, 60.0f},
    .accel_mm_s2 = 500.0f,
    .move_timeout_ms = 15000u,
    .grip_timeout_ms = 3000u,
    .home_timeout_ms = 20000u,
};

/* ------------------------------------------------------------------ proto */

static void test_proto(void)
{
    cmd_t c;

    printf("proto\n");

    CHECK(proto_parse("PING", &c) == 0 && c.kind == CMD_PING);
    CHECK(proto_parse("ping", &c) == 0 && c.kind == CMD_PING);
    CHECK(proto_parse("HOME", &c) == 0 && c.kind == CMD_HOME);
    CHECK(proto_parse("STOP", &c) == 0 && c.kind == CMD_STOP);

    CHECK(proto_parse("GOTO 30.0 40.5", &c) == 0 && c.kind == CMD_GOTO);
    CHECK_NEAR(c.a, 30.0f, 1e-4f);
    CHECK_NEAR(c.b, 40.5f, 1e-4f);

    CHECK(proto_parse("Z 12.3", &c) == 0 && c.kind == CMD_Z);
    CHECK_NEAR(c.a, 12.3f, 1e-4f);

    CHECK(proto_parse("GRIP 6.0", &c) == 0 && c.kind == CMD_GRIP);
    CHECK_NEAR(c.a, 6.0f, 1e-4f);

    /* 负数（回零后可能给负的目标，用来顶限位） */
    CHECK(proto_parse("Z -3.5", &c) == 0 && c.kind == CMD_Z);
    CHECK_NEAR(c.a, -3.5f, 1e-4f);

    /* 多空格 / 制表符 / 行尾 \r（Windows 串口常带） */
    CHECK(proto_parse("  GOTO  1.0   2.0  ", &c) == 0 && c.kind == CMD_GOTO);
    CHECK(proto_parse("PING\r", &c) == 0 && c.kind == CMD_PING);

    /* 错误路径 */
    CHECK(proto_parse("", &c) == PROTO_E_EMPTY);
    CHECK(proto_parse("   ", &c) == PROTO_E_EMPTY);
    CHECK(proto_parse("BOGUS", &c) == PROTO_E_CMD);
    CHECK(proto_parse("GOTO 1.0", &c) == PROTO_E_ARGS);
    CHECK(proto_parse("GOTO 1.0 2.0 3.0", &c) == PROTO_E_ARGS);
    CHECK(proto_parse("GOTO a b", &c) == PROTO_E_NUM);
    CHECK(proto_parse("PING 1", &c) == PROTO_E_ARGS);
    CHECK(proto_parse("Z -", &c) == PROTO_E_NUM);
    CHECK(proto_parse("Z 1.2.3", &c) == PROTO_E_NUM);
    CHECK(proto_parse("GOTO 1.0 2.0 extra", &c) == PROTO_E_ARGS);

    /* 组包 */
    {
        char buf[PROTO_LINE_MAX];
        CHECK(proto_fmt_ack(buf, sizeof buf, "OK") == 2 && strcmp(buf, "OK") == 0);
        CHECK(proto_fmt_gripd(buf, sizeof buf, 18.0f) > 0
              && strcmp(buf, "GRIPD 18.0") == 0);
        CHECK(proto_fmt_gripd(buf, sizeof buf, 0.0f) > 0
              && strcmp(buf, "GRIPD 0.0") == 0);
        CHECK(proto_fmt_gripd(buf, sizeof buf, 6.04f) > 0
              && strcmp(buf, "GRIPD 6.0") == 0);
        CHECK(proto_fmt_gripd(buf, sizeof buf, 6.05f) > 0
              && strcmp(buf, "GRIPD 6.1") == 0);
        CHECK(proto_fmt_err(buf, sizeof buf, "E01", "out of range") > 0
              && strcmp(buf, "ERR E01 out of range") == 0);
        CHECK(proto_fmt_err(buf, sizeof buf, "E03", NULL) > 0
              && strcmp(buf, "ERR E03") == 0);
        /* 缓冲区太小必须拒绝，不能截断出一个半截帧 */
        CHECK(proto_fmt_ack(buf, 2, "OK") == -1);
    }
}

/* ------------------------------------------------------------------ motion */

static void test_motion(void)
{
    motion_cfg_t cfg = {200.0f, 500.0f};   /* 200 mm/s, 500 mm/s² */
    motion_plan_t p;
    float d_min;

    printf("motion\n");

    /* 长距离一定是梯形。100mm 需要加速距离 200²/500 = 80mm，够。 */
    motion_plan(100.0f, &cfg, &p);
    CHECK(p.triangular == 0);
    CHECK_NEAR(p.v_peak_mm_s, 200.0f, 1e-3f);
    CHECK_NEAR(p.t_accel_s, 0.4f, 1e-4f);
    /* t = d/v + v/a = 0.5 + 0.4 = 0.9 */
    CHECK_NEAR(p.t_total_s, 0.9f, 1e-4f);

    /* 短距离一定是三角形 */
    motion_plan(10.0f, &cfg, &p);
    CHECK(p.triangular == 1);
    CHECK_NEAR(p.v_peak_mm_s, sqrtf(500.0f * 10.0f), 1e-3f);   /* ≈70.7 */
    CHECK(p.v_peak_mm_s < 200.0f);

    /* 临界点：d = v²/a = 80mm。两侧公式必须连续，不能跳变。 */
    d_min = 200.0f * 200.0f / 500.0f;
    motion_plan(d_min - 0.01f, &cfg, &p);
    CHECK(p.triangular == 1);
    {
        float t_lo = p.t_total_s;
        motion_plan(d_min + 0.01f, &cfg, &p);
        CHECK(p.triangular == 0);
        CHECK_NEAR(p.t_total_s, t_lo, 2e-3f);
    }

    /* 负距离按绝对值处理（换向由 steps 的符号表达） */
    motion_plan(-100.0f, &cfg, &p);
    CHECK_NEAR(p.t_total_s, 0.9f, 1e-4f);

    /* 零距离 / 非法参数 → 全零，不能算出 nan */
    motion_plan(0.0f, &cfg, &p);
    CHECK_NEAR(p.t_total_s, 0.0f, 1e-6f);
    {
        motion_cfg_t bad = {0.0f, 500.0f};
        motion_plan(50.0f, &bad, &p);
        CHECK_NEAR(p.t_total_s, 0.0f, 1e-6f);
        CHECK(p.v_peak_mm_s == 0.0f);
    }

    /* 脉冲周期：200mm/s × 50.93 步/mm = 10186 脉冲/s -> 98.2us */
    CHECK_NEAR(motion_step_period_us(200.0f, 50.93f), 98.17f, 0.1f);
    CHECK(motion_step_period_us(0.0f, 50.93f) == 0.0f);

    /* 时间必须是单调的：距离越大越久 */
    {
        float prev = -1.0f;
        int i;
        for (i = 0; i <= 40; i++) {
            motion_plan((float)i * 5.0f, &cfg, &p);
            CHECK(p.t_total_s >= prev);
            prev = p.t_total_s;
        }
    }
}

/* ------------------------------------------------------------------ machine */

static machine_t M;

static void start(void)
{
    reset_fake();
    machine_init(&M, &FAKE_HAL, &CFG, 0u);
}

static void test_machine(void)
{
    printf("machine\n");

    /* PING -> OK, PONG */
    start();
    machine_on_line(&M, "PING", 0u);
    CHECK(g_nreply == 2);
    CHECK(strcmp(g_reply[0], "OK") == 0);
    CHECK(strcmp(g_reply[1], "PONG") == 0);

    /* GOTO -> OK，走完 -> DONE。两根轴同时启动。 */
    start();
    machine_on_line(&M, "GOTO 100.0 50.0", 0u);
    CHECK(g_nreply == 1 && strcmp(g_reply[0], "OK") == 0);
    CHECK(M.state == M_MOVING);
    CHECK(g_moving[AX_X] == 1 && g_moving[AX_Y] == 1);
    /* 100mm × 50.93 步/mm ≈ 5093 步 */
    CHECK(g_last_steps[AX_X] == 5093);
    finish_motion(&M, 100.0f, 50.0f, 0.0f, 0.0f);
    CHECK(g_nreply == 2 && strcmp(g_reply[1], "DONE") == 0);
    CHECK(M.state == M_IDLE);

    /* 夹爪 -> OK, DONE, GRIPD。GRIPD 必须是实际停止位置，不是指令位置。 */
    start();
    machine_on_line(&M, "GRIP 6.0", 0u);
    CHECK(M.state == M_GRIP);
    g_grip_actual = 18.0f;          /* 堵在工件上 */
    finish_motion(&M, 0.0f, 0.0f, 0.0f, 18.0f);
    CHECK(g_nreply == 3);
    CHECK(strcmp(g_reply[1], "DONE") == 0);
    CHECK(strcmp(g_reply[2], "GRIPD 18.0") == 0);

    /* 超程 -> ERR E01 且进 FAULT */
    start();
    machine_on_line(&M, "GOTO 999.0 0.0", 0u);
    CHECK(g_nreply == 1 && strncmp(g_reply[0], "ERR E01", 7) == 0);
    CHECK(M.state == M_FAULT);

    /* 进 FAULT 后任何指令都回 ERR，不再动 */
    machine_on_line(&M, "PING", 10u);
    CHECK(g_nreply == 2 && strncmp(g_reply[1], "ERR", 3) == 0);

    /* 未知指令 -> E03，但**不进 FAULT**（拼错一行不该停机） */
    start();
    machine_on_line(&M, "HELLO", 0u);
    CHECK(strncmp(g_reply[0], "ERR E03", 7) == 0);
    CHECK(M.state == M_IDLE);

    /* 数字格式错也是 E03，不进 FAULT */
    start();
    machine_on_line(&M, "Z abc", 0u);
    CHECK(strncmp(g_reply[0], "ERR E03", 7) == 0);
    CHECK(M.state == M_IDLE);

    /* HOME -> OK，走完 -> DONE */
    start();
    machine_on_line(&M, "HOME", 0u);
    CHECK(strcmp(g_reply[0], "OK") == 0);
    CHECK(M.state == M_HOMING);
    memset(g_moving, 0, sizeof g_moving);
    machine_tick(&M, 100u);
    CHECK(strcmp(g_reply[1], "DONE") == 0);

    /* 找限位失败 -> E02 + FAULT */
    start();
    machine_on_line(&M, "HOME", 0u);
    g_home_failed = 1;
    machine_tick(&M, 100u);
    CHECK(strncmp(g_reply[1], "ERR E02", 7) == 0);
    CHECK(M.state == M_FAULT);

    /* STOP -> OK 并清掉运动 */
    start();
    machine_on_line(&M, "GOTO 100.0 0.0", 0u);
    machine_on_line(&M, "STOP", 10u);
    CHECK(strcmp(g_reply[1], "OK") == 0);
    CHECK(M.state == M_IDLE);
    CHECK(g_moving[AX_X] == 0);

    /* 运动超时 -> ERR，不死等 */
    start();
    machine_on_line(&M, "Z 10.0", 0u);
    machine_tick(&M, 20000u);       /* move_timeout 是 15s */
    CHECK(g_nreply == 2);
    CHECK(strncmp(g_reply[1], "ERR E01", 7) == 0);

    /* 夹爪超时 -> E04，且**不进 FAULT**（卡住一件不该停整台机器） */
    start();
    machine_on_line(&M, "GRIP 6.0", 0u);
    machine_tick(&M, 4000u);        /* grip_timeout 是 3s */
    CHECK(strncmp(g_reply[1], "ERR E04", 7) == 0);
    CHECK(M.state == M_IDLE);

    /* 目标就是当前位置 -> 不动，但要回 OK + DONE */
    start();
    machine_on_line(&M, "GOTO 0.0 0.0", 0u);
    CHECK(strcmp(g_reply[0], "OK") == 0);
    machine_tick(&M, 10u);
    CHECK(strcmp(g_reply[1], "DONE") == 0);
}

/* ------------------------------------------------------------------ main */

int main(void)
{
    test_proto();
    test_motion();
    test_machine();

    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    if (g_fail != 0) {
        printf("FAILED\n");
        return 1;
    }
    printf("OK\n");
    return 0;
}
